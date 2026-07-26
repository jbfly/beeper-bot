"""Matrix (matrix-nio) transport implementing the beeper_api.py surface.

This is the venus-side replacement for the Beeper Desktop HTTP API: it talks
straight to Beeper's homeserver (hungryserv) as a dedicated, cross-signed
Matrix device, so the bot can run headless next to the self-hosted bridges
instead of depending on Beeper Desktop's GUI on alpha. See
docs/self-hosted-bridges-and-matrix-migration.md.

Design notes
------------
- matrix-nio is async; the rest of the bot is synchronous. We run a single
  persistent AsyncClient on its own event loop in a daemon thread and marshal
  each call across with run_coroutine_threadsafe. Keeping one client alive
  preserves the E2EE store, sync token, and room state between calls (a fresh
  client per call would re-sync every time).
- The returned chat/message dicts mirror the Beeper Desktop payload shape that
  sync.py / bridge.py / discovery.py already consume (id/title/lastActivity;
  sortKey/type/text/senderID/senderName/isSender/attachments). sortKey is the
  event's origin_server_ts (monotonic per room; ties break on message_id).
- Encrypted history the device has no megolm key for is skipped (shows up as
  undecryptable). Restoring the key backup is a separate step; until then the
  archive only sees messages sent after the device joined.
- nio requires Python 3.10 for python-olm; imports are lazy so this module can
  be imported anywhere without pulling nio into the default runtime.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .beeper_api import BeeperApiError, MessagePage
from .config import BeeperConfig

DEFAULT_CREDENTIALS = Path.home() / ".config" / "beeper-bot" / "matrix-credentials.json"
DEFAULT_STORE = Path.home() / ".local" / "state" / "beeper-bot" / "matrix-store"

# How many events a single /messages page pulls when no explicit limit applies.
_PAGE_LIMIT = 500

# Cap on the live per-room message buffer kept from sync.
_RECENT_CAP = 400

# Seconds to wait on a cross-thread transport call (and on startup readiness).
_CALL_TIMEOUT = 120

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


# Tiebreaker width for sort keys. Matrix origin_server_ts is not unique (bridge
# backfill and photo albums share a millisecond), but the archive requires a
# unique (chat_id, sort_key). Pack ts_ms into the high bits and a stable
# per-event hash into the low 20 bits: ordering stays by time, ties break
# deterministically, and ts_ms * 2^20 stays within SQLite's signed-64-bit range.
_SORTKEY_TIE_BITS = 20


def _sort_key(ts_ms: int, event_id: str) -> int:
    tie = int.from_bytes(hashlib.sha1(event_id.encode()).digest()[:3], "big") & ((1 << _SORTKEY_TIE_BITS) - 1)
    return (int(ts_ms) << _SORTKEY_TIE_BITS) | tie


def _load_credentials(config: BeeperConfig) -> dict:
    creds_path = Path(config.matrix_credentials_file or DEFAULT_CREDENTIALS)
    if not creds_path.exists():
        raise BeeperApiError(f"Matrix credentials not found: {creds_path}")
    return json.loads(creds_path.read_text())


class MatrixTransport:
    """Synchronous facade over a persistent matrix-nio client."""

    def __init__(self, config: BeeperConfig, allow_send: bool = False):
        self.config = config
        self.allow_send = allow_send
        self._creds = _load_credentials(config)
        self._store = Path(config.matrix_store_path or DEFAULT_STORE)
        self._store.mkdir(parents=True, exist_ok=True)
        self._own_id = str(self._creds["user_id"])
        # room_id -> last event origin_server_ts (ms), maintained by the sync loop
        self._last_activity: dict[str, int] = {}
        # room_id -> recent mapped messages (live tail from sync), newest last
        self._recent: dict[str, deque] = {}
        # room_id -> backward pagination token (per-room prev_batch); hungryserv
        # rejects the global sync token as a /messages `from`, so we seed each
        # room's history walk with the prev_batch captured when we first saw it.
        self._back_token: dict[str, str] = {}
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._thread = threading.Thread(target=self._run_loop, name="matrix-transport", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=_CALL_TIMEOUT):
            raise BeeperApiError("Matrix transport did not become ready in time")
        if self._start_error is not None:
            raise BeeperApiError(f"Matrix transport failed to start: {self._start_error}")

    # ---- event loop / client lifecycle -------------------------------------

    def _run_loop(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._bootstrap())
        except BaseException as exc:  # surface to constructor
            self._start_error = exc
            self._ready.set()
            return
        # keep serving calls + background sync until the process exits
        loop.run_forever()

    async def _bootstrap(self) -> None:
        from nio import AsyncClient, AsyncClientConfig, SyncResponse

        cfg = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)
        client = AsyncClient(
            self._creds["homeserver"],
            self._creds["user_id"],
            device_id=self._creds["device_id"],
            store_path=str(self._store),
            config=cfg,
        )
        client.restore_login(
            user_id=self._creds["user_id"],
            device_id=self._creds["device_id"],
            access_token=self._creds["access_token"],
        )
        self._client = client
        # Response callbacks only fire inside sync_forever, not the manual
        # sync() below, so we ingest the initial response by hand and let the
        # callback handle every sync after that.
        client.add_response_callback(self._on_sync, SyncResponse)

        # hungryserv incremental sync only returns recently-active rooms, so a
        # resumed sync token hides quiet rooms (e.g. the control chat). Force a
        # full initial sync so every joined room is known before we serve calls.
        client.loaded_sync_token = ""
        client.next_batch = None
        resp = await client.sync(timeout=30000, full_state=True)
        self._ingest_sync(resp)
        if client.should_upload_keys:
            await client.keys_upload()
        self._ready.set()
        # background sync keeps rooms/timelines and to-device (keys) current
        asyncio.create_task(client.sync_forever(timeout=30000, full_state=False))

    async def _on_sync(self, response) -> None:
        self._ingest_sync(response)

    def _ingest_sync(self, response) -> None:
        """Capture per-room recent messages, last-activity, and back-tokens."""
        for room_id, joined in response.rooms.join.items():
            room = self._client.rooms.get(room_id)
            timeline = joined.timeline
            # first time we see the room, remember the token to walk older history
            if room_id not in self._back_token and getattr(timeline, "prev_batch", None):
                self._back_token[room_id] = timeline.prev_batch
            buf = self._recent.setdefault(room_id, deque(maxlen=_RECENT_CAP))
            for event in timeline.events:
                ts = getattr(event, "server_timestamp", 0) or 0
                if ts > self._last_activity.get(room_id, 0):
                    self._last_activity[room_id] = ts
                if room is not None:
                    mapped = self._event_to_message(room, event)
                    if mapped is not None:
                        buf.append(mapped)

    def _submit(self, coro):
        if self._loop is None:
            raise BeeperApiError("Matrix transport loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=_CALL_TIMEOUT)

    # ---- mapping -----------------------------------------------------------

    def _display_name(self, room, mxid: str) -> str:
        return str(room.user_name(mxid) or mxid)

    def _event_to_message(self, room, event) -> dict[str, Any] | None:
        """Map a nio timeline event to a Beeper-shaped message dict, or None."""
        from nio import (
            RoomMessageText,
            RoomMessageNotice,
            RoomMessageEmote,
            RoomMessageImage,
            RoomMessageAudio,
            RoomMessageVideo,
            RoomMessageFile,
        )

        text_types = (RoomMessageText, RoomMessageNotice, RoomMessageEmote)
        media_map = [
            (RoomMessageImage, "IMAGE", "img"),
            (RoomMessageAudio, "VOICE", "audio"),
            (RoomMessageVideo, "VIDEO", "video"),
            (RoomMessageFile, "FILE", "file"),
        ]
        sender = getattr(event, "sender", "") or ""
        ts = getattr(event, "server_timestamp", 0) or 0
        event_id = getattr(event, "event_id", "") or ""
        base = {
            "id": event_id,
            "messageID": event_id,
            "sortKey": _sort_key(ts, event_id),
            "timestamp": _iso(int(ts)),
            "senderID": sender,
            "senderName": self._display_name(room, sender),
            "isSender": sender == self._own_id,
        }
        if isinstance(event, text_types):
            base["type"] = "TEXT"
            base["text"] = getattr(event, "body", "") or ""
            return base
        for cls, beeper_type, att_type in media_map:
            if isinstance(event, cls):
                content = (getattr(event, "source", None) or {}).get("content", {})
                info = content.get("info", {})
                # mxc lives at content.url (plaintext) or content.file.url (encrypted)
                mxc = content.get("url") or (content.get("file", {}) or {}).get("url") or getattr(event, "url", "")
                body = getattr(event, "body", "") or content.get("body") or "attachment"
                is_voice = "org.matrix.msc3245.voice" in content or "org.matrix.msc1767.audio" in content
                base["type"] = beeper_type
                base["text"] = None
                base["attachments"] = [
                    {
                        "id": event_id,
                        "srcURL": mxc,
                        "fileName": body,
                        "type": att_type,
                        "mimeType": info.get("mimetype") or content.get("mimetype") or "",
                        "fileSize": info.get("size") or 0,
                        "isVoiceNote": bool(is_voice),
                        # decryption material for encrypted attachments (step 2)
                        "encFile": content.get("file"),
                    }
                ]
                return base
        # undecryptable megolm events / state / unknown → skip
        return None

    def _room_to_chat(self, room) -> dict[str, Any]:
        return {
            "id": room.room_id,
            "title": room.display_name or room.room_id,
            "name": room.display_name or room.room_id,
            "lastActivity": _iso(self._last_activity.get(room.room_id, 0)),
        }

    # ---- async workers -----------------------------------------------------

    async def _a_fetch_chat(self, chat_id: str) -> dict[str, Any]:
        room = self._client.rooms.get(chat_id)
        if room is None:
            raise BeeperApiError(f"Unknown Matrix room: {chat_id}")
        return self._room_to_chat(room)

    async def _a_fetch_all_chats(self) -> list[dict[str, Any]]:
        return [self._room_to_chat(r) for r in self._client.rooms.values()]

    async def _a_fetch_messages_page(self, chat_id: str, cursor: str | None, direction: str | None) -> MessagePage:
        from nio import MessageDirection, RoomMessagesError

        room = self._client.rooms.get(chat_id)
        if room is None:
            raise BeeperApiError(f"Unknown Matrix room: {chat_id}")

        # First page (no cursor): serve the live tail from the sync buffer and
        # hand back the per-room token to walk older history. Beeper's sync
        # dedups by message id, so any overlap with the first history page is
        # harmless.
        if not cursor:
            buf = self._recent.get(chat_id) or deque()
            items = list(buf)[-_PAGE_LIMIT:]
            back = self._back_token.get(chat_id)
            return MessagePage(
                items=items,
                has_more=bool(back),
                oldest_cursor=back,
                newest_cursor=None,
            )

        # History pages: paginate backward from the supplied per-room token.
        resp = await self._client.room_messages(
            chat_id,
            start=cursor,
            direction=MessageDirection.back,
            limit=_PAGE_LIMIT,
        )
        if isinstance(resp, RoomMessagesError):
            raise BeeperApiError(f"Matrix /messages failed for {chat_id}: {resp.message}")
        items = []
        for event in resp.chunk:
            mapped = self._event_to_message(room, event)
            if mapped is not None:
                items.append(mapped)
        # resp.end is the token to continue paginating backward (older); when the
        # server returns no further events we are at the start of history.
        has_more = bool(resp.end) and resp.end != cursor and bool(resp.chunk)
        return MessagePage(
            items=items,
            has_more=has_more,
            oldest_cursor=resp.end or None,
            newest_cursor=resp.start or None,
        )

    async def _a_send_message(self, chat_id: str, text: str) -> None:
        from nio import RoomSendError

        resp = await self._client.room_send(
            chat_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
            ignore_unverified_devices=True,
        )
        if isinstance(resp, RoomSendError):
            raise BeeperApiError(f"Matrix send failed for {chat_id}: {resp.message}")

    # ---- BeeperApiClient-compatible sync surface ---------------------------

    def fetch_chat(self, chat_id: str) -> dict[str, Any]:
        return self._submit(self._a_fetch_chat(chat_id))

    def fetch_all_chats(self) -> list[dict[str, Any]]:
        return self._submit(self._a_fetch_all_chats())

    def fetch_messages_page(self, chat_id: str, cursor: str | None = None, direction: str | None = None) -> MessagePage:
        return self._submit(self._a_fetch_messages_page(chat_id, cursor, direction))

    def fetch_messages(self, chat_id: str) -> list[dict[str, Any]]:
        return self.fetch_messages_page(chat_id).items

    def send_message(self, chat_id: str, text: str) -> None:
        if not self.allow_send:
            raise PermissionError("sending disabled: set security.allow_send = true to enable")
        self._submit(self._a_send_message(chat_id, text))


def download_attachment(config: BeeperConfig, attachment: dict[str, Any]) -> bytes:
    """Download an mxc attachment from the homeserver and decrypt it.

    Handles Beeper's encrypted attachments (the message dict carries the MSC3244
    `file` block under `encFile`) as well as plaintext ones. This is the matrix
    transport's replacement for Beeper Desktop's /assets/serve endpoint. Uses a
    short-lived nio client (no sync) because Beeper's media routing needs nio's
    download handling; a plain GET on the media path returns HTTP 400.
    """
    from nio import AsyncClient, AsyncClientConfig
    from nio.crypto.attachments import decrypt_attachment

    creds = _load_credentials(config)
    enc = attachment.get("encFile")
    mxc = (enc or {}).get("url") if enc else attachment.get("srcURL")
    if not mxc or not str(mxc).startswith("mxc://"):
        raise BeeperApiError(f"Attachment has no mxc url: {str(mxc)[:40]}")
    server, media_id = str(mxc)[len("mxc://"):].split("/", 1)

    async def _download() -> bytes:
        client = AsyncClient(
            creds["homeserver"], creds["user_id"], device_id=creds["device_id"],
            config=AsyncClientConfig(encryption_enabled=False),
        )
        client.restore_login(
            user_id=creds["user_id"], device_id=creds["device_id"], access_token=creds["access_token"],
        )
        try:
            resp = await client.download(server_name=server, media_id=media_id)
            if not hasattr(resp, "body"):
                raise BeeperApiError(f"Attachment download failed: {getattr(resp, 'message', resp)}")
            return resp.body
        finally:
            await client.close()

    loop = asyncio.new_event_loop()
    try:
        ciphertext = loop.run_until_complete(_download())
    finally:
        loop.close()
    if not ciphertext:
        raise BeeperApiError("Attachment download returned no data")
    if enc:
        return decrypt_attachment(ciphertext, enc["key"]["k"], enc["hashes"]["sha256"], enc["iv"])
    return ciphertext


# ---- one-shot megolm key-backup restore -----------------------------------
#
# The nio device can't decrypt history from before it joined a room. Beeper
# keeps a server-side megolm backup (m.megolm_backup.v1.curve25519-aes-sha2)
# whose decryption key is sealed in secret storage (SSSS) under the account
# recovery key. This restores it once into the nio store so the transport can
# read old history. See docs/self-hosted-bridges-and-matrix-migration.md.


def _b58decode(text: str) -> bytes:
    n = 0
    for ch in text.replace(" ", ""):
        n = n * 58 + _BASE58.index(ch)
    return n.to_bytes(35, "big")


def _recovery_key_to_ssss_key(recovery_key: str) -> bytes:
    raw = _b58decode(recovery_key)
    if raw[0] != 0x8B or raw[1] != 0x01:
        raise BeeperApiError("Recovery key has an unexpected prefix")
    parity = 0
    for byte in raw:
        parity ^= byte
    if parity != 0:
        raise BeeperApiError("Recovery key failed its parity check")
    return raw[2:34]


def _hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _pk_from_private(private_key: bytes):
    """Build an olm.PkDecryption bound to an existing curve25519 private key."""
    import olm
    from olm import pk as pkmod

    lib, ffi = pkmod.lib, pkmod.ffi
    obj = olm.PkDecryption.__new__(olm.PkDecryption)
    key_len = lib.olm_pk_key_length()
    key_buf = ffi.new("char[]", key_len)
    priv_buf = ffi.new("char[]", bytes(private_key))
    obj._check_error(
        lib.olm_pk_key_from_private(obj._pk_decryption, key_buf, key_len, priv_buf, len(private_key))
    )
    obj.public_key = ffi.unpack(key_buf, key_len).decode()
    return obj


def restore_key_backup(config: BeeperConfig, recovery_key: str) -> dict[str, int]:
    """Decrypt the server-side megolm backup and import it into the nio store.

    One-shot: run once (the store persists). Idempotent. Returns counts of
    total / imported (decrypted and written) / failed sessions.
    """
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
    from nio import AsyncClient, AsyncClientConfig
    from nio.crypto import InboundGroupSession

    creds = _load_credentials(config)
    homeserver = creds["homeserver"]
    user_id = creds["user_id"]
    token = creds["access_token"]

    def _get(path: str) -> Any:
        req = urllib.request.Request(homeserver + path, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=config.http_timeout_seconds or 60) as resp:
            return json.loads(resp.read().decode())

    def _account_data(name: str) -> dict:
        return _get(f"/_matrix/client/v3/user/{user_id}/account_data/{name}")

    ssss_key = _recovery_key_to_ssss_key(recovery_key)
    key_id = _account_data("m.secret_storage.default_key")["key"]
    secret = _account_data("m.megolm_backup.v1")["encrypted"][key_id]
    okm = _hkdf_sha256(ssss_key, b"\x00" * 32, b"m.megolm_backup.v1", 64)
    ciphertext = base64.b64decode(secret["ciphertext"])
    if not hmac.compare_digest(base64.b64decode(secret["mac"]), hmac.new(okm[32:64], ciphertext, hashlib.sha256).digest()):
        raise BeeperApiError("Recovery key does not match this account's secret storage")
    counter = Counter.new(128, initial_value=int.from_bytes(base64.b64decode(secret["iv"]), "big"))
    backup_private = base64.b64decode(AES.new(okm[:32], AES.MODE_CTR, counter=counter).decrypt(ciphertext).decode().strip())

    import olm

    pk = _pk_from_private(backup_private)
    version = _get("/_matrix/client/v3/room_keys/version")
    if pk.public_key.rstrip("=") != version["auth_data"]["public_key"].rstrip("="):
        raise BeeperApiError("Derived backup key does not match the server's backup version")
    rooms = _get(f"/_matrix/client/v3/room_keys/keys?version={version['version']}").get("rooms", {})

    store = Path(config.matrix_store_path or DEFAULT_STORE)
    store.mkdir(parents=True, exist_ok=True)
    client = AsyncClient(
        homeserver, user_id, device_id=creds["device_id"], store_path=str(store),
        config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True),
    )
    client.restore_login(user_id=user_id, device_id=creds["device_id"], access_token=token)
    client.load_store()

    imported = failed = 0
    for room_id, room in rooms.items():
        for session_data in room.get("sessions", {}).values():
            try:
                data = json.loads(pk.decrypt(olm.PkMessage(
                    session_data["session_data"]["ephemeral"],
                    session_data["session_data"]["mac"],
                    session_data["session_data"]["ciphertext"],
                )))
                session = InboundGroupSession.import_session(
                    data["session_key"],
                    data["sender_claimed_keys"].get("ed25519", ""),
                    data["sender_key"],
                    room_id,
                    data.get("forwarding_curve25519_key_chain", []),
                )
                if client.olm.inbound_group_store.add(session):
                    client.olm.save_inbound_group_session(session)
                imported += 1
            except Exception:
                failed += 1
    total = sum(len(r.get("sessions", {})) for r in rooms.values())
    return {"total": total, "imported": imported, "failed": failed}


def create_chat(config: BeeperConfig, name: str, topic: str = "", encrypted: bool = True) -> dict:
    """Create a new Matrix room to serve as a purpose-scoped control chat.

    Mints a private room owned by the bot's own account (a "note to self" with a
    name), so purpose chats can be created headlessly on venus without the Beeper
    GUI. The room is server-side state: a running serve loop picks it up on its
    next sync, so this is safe to call while the bot is running (no shared nio
    store is opened here — it's a raw authenticated createRoom call).

    Returns {"room_id", "name", "encrypted"}. Add the room_id under
    [control_chats.<name>] in config and restart serve to start polling it.
    """
    creds = _load_credentials(config)
    homeserver = creds["homeserver"].rstrip("/")
    token = creds["access_token"]
    body: dict[str, Any] = {
        "name": name,
        "preset": "private_chat",
        "visibility": "private",
        "is_direct": False,
    }
    if topic:
        body["topic"] = topic
    if encrypted:
        body["initial_state"] = [{
            "type": "m.room.encryption",
            "state_key": "",
            "content": {"algorithm": "m.megolm.v1.aes-sha2"},
        }]
    req = urllib.request.Request(
        homeserver + "/_matrix/client/v3/createRoom",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=config.http_timeout_seconds or 60) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        detail = exc.read().decode("utf-8", errors="replace")
        raise BeeperApiError(f"createRoom failed: HTTP {exc.code} {detail}") from exc
    room_id = str(result.get("room_id") or "")
    if not room_id:
        raise BeeperApiError(f"createRoom returned no room_id: {result}")
    return {"room_id": room_id, "name": name, "encrypted": encrypted}
