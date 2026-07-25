from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .beeper_api import MessagePage
from .config import AppConfig
from .db import get_runtime_state, init_db_path, open_db, set_runtime_state, utc_now


BACKFILL_DONE_KEY_PREFIX = "sync_backfill_done:"


class SyncClient(Protocol):
    def fetch_chat(self, chat_id: str) -> dict[str, Any]: ...
    def fetch_messages(self, chat_id: str) -> list[dict[str, Any]]: ...
    def fetch_messages_page(self, chat_id: str, cursor: str | None = None, direction: str | None = None) -> MessagePage: ...


@dataclass(slots=True)
class ChatSyncResult:
    chat_id: str
    chat_name: str
    fetched_messages: int
    stored_messages: int
    latest_sort_key: int | None


@dataclass(slots=True)
class SyncResult:
    chats: list[ChatSyncResult]

    @property
    def total_fetched_messages(self) -> int:
        return sum(chat.fetched_messages for chat in self.chats)

    @property
    def total_stored_messages(self) -> int:
        return sum(chat.stored_messages for chat in self.chats)


def normalize_text(text: str | None) -> str | None:
    if text is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = re.sub(r"[ \t\f\v]+", " ", normalized)
    return normalized



def normalized_evidence_fingerprint(text: str | None, sender_name: str | None, timestamp: str | None) -> str:
    normalized_body = (normalize_text(text) or "").casefold()
    normalized_sender = (normalize_text(sender_name) or "").casefold()
    timestamp_value = str(timestamp or "").strip()
    try:
        normalized_timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0).isoformat(timespec="seconds")
    except ValueError:
        normalized_timestamp = timestamp_value[:19]
    if not normalized_body or not normalized_sender or not normalized_timestamp:
        return ""
    return hashlib.sha256("\0".join((normalized_body, normalized_sender, normalized_timestamp)).encode()).hexdigest()


def find_possible_duplicate(conn: sqlite3.Connection, chat_id: str, message_id: str, source_kind: str, fingerprint: str) -> str | None:
    if not fingerprint:
        return None
    row = conn.execute(
        """SELECT m.message_id FROM messages m JOIN chats c ON c.chat_id = m.chat_id
           WHERE m.chat_id = ? AND m.message_id != ? AND m.source_kind != ?
             AND m.evidence_fingerprint = ? AND c.is_allowed = 1
           ORDER BY m.sort_key LIMIT 1""",
        (chat_id, message_id, source_kind, fingerprint),
    ).fetchone()
    return str(row["message_id"]) if row else None

def _message_text(message: dict[str, Any]) -> str | None:
    text = message.get("text")
    if text not in (None, ""):
        return str(text)
    if message.get("type") == "IMAGE":
        attachments = message.get("attachments", [])
        if attachments and isinstance(attachments, list):
            file_name = attachments[0].get("fileName") if isinstance(attachments[0], dict) else None
            return f"[image: {file_name or 'attachment'}]"
    return None


def _sort_key(message: dict[str, Any]) -> int | None:
    value = message.get("sortKey")
    if value in (None, ""):
        return None
    return int(value)


def _message_id(chat_id: str, message: dict[str, Any], sort_key: int | None) -> str:
    for key in ("messageID", "messageId", "id", "eventID", "eventId"):
        value = message.get(key)
        if value not in (None, ""):
            return str(value)
    if sort_key is not None:
        return f"{chat_id}:{sort_key}"
    digest = hashlib.sha256(json.dumps(message, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{chat_id}:sha256:{digest}"


def _chat_name(chat_payload: dict[str, Any], chat_id: str) -> str:
    for key in ("title", "name", "displayName"):
        value = chat_payload.get(key)
        if value not in (None, ""):
            return str(value)
    return chat_id


def _upsert_chat(conn: sqlite3.Connection, chat_id: str, chat_name: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO chats(chat_id, name, is_allowed, approval_source, approved_at, created_at, updated_at, last_synced_at)
        VALUES (?, ?, 0, '', NULL, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            name = excluded.name,
            updated_at = excluded.updated_at,
            last_synced_at = excluded.last_synced_at
        """,
        (chat_id, chat_name, now, now, now),
    )
    conn.execute("UPDATE message_fts SET chat_name = ? WHERE chat_id = ?", (chat_name, chat_id))


def _upsert_message(conn: sqlite3.Connection, chat_id: str, chat_name: str, message: dict[str, Any]) -> bool:
    sort_key = _sort_key(message)
    if sort_key is None:
        return False
    message_id = _message_id(chat_id, message, sort_key)
    text = _message_text(message)
    timestamp = str(message.get("timestamp", ""))
    sender_name = str(message.get("senderName", "") or "")
    raw_json = json.dumps(message, sort_keys=True)
    artifact_sha256 = hashlib.sha256(raw_json.encode()).hexdigest()
    fingerprint = normalized_evidence_fingerprint(text, sender_name, timestamp)
    possible_duplicate = find_possible_duplicate(conn, chat_id, message_id, "beeper", fingerprint)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO messages(
            message_id, chat_id, sort_key, timestamp, sender_id, sender_name,
            is_sender, message_type, text, normalized_text, raw_json, source_kind, source_ref,
            source_artifact_sha256, evidence_fingerprint, possible_duplicate_of, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'beeper', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            chat_id = excluded.chat_id,
            sort_key = excluded.sort_key,
            timestamp = excluded.timestamp,
            sender_id = excluded.sender_id,
            sender_name = excluded.sender_name,
            is_sender = excluded.is_sender,
            message_type = excluded.message_type,
            text = excluded.text,
            normalized_text = excluded.normalized_text,
            raw_json = excluded.raw_json,
            source_artifact_sha256 = excluded.source_artifact_sha256,
            evidence_fingerprint = excluded.evidence_fingerprint,
            possible_duplicate_of = COALESCE(excluded.possible_duplicate_of, messages.possible_duplicate_of),
            updated_at = excluded.updated_at
        """,
        (
            message_id,
            chat_id,
            sort_key,
            timestamp,
            str(message.get("senderID", "") or ""),
            sender_name,
            1 if message.get("isSender") else 0,
            str(message.get("type", "UNKNOWN")),
            text,
            normalize_text(text),
            raw_json,
            message_id,
            artifact_sha256,
            fingerprint,
            possible_duplicate,
            now,
            now,
        ),
    )
    conn.execute("DELETE FROM message_fts WHERE message_id = ?", (message_id,))
    if text:
        conn.execute(
            """
            INSERT INTO message_fts(message_id, chat_id, chat_name, sender_name, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, chat_id, chat_name, sender_name, text),
        )
    # Media messages carry transcript/description text derived after ingest;
    # the upsert above just overwrote it with the raw payload text, so put it
    # back.
    if str(message.get("type", "")) in ("VOICE", "IMAGE", "VIDEO", "FILE"):
        from .media import reapply_derived_text

        reapply_derived_text(conn, message_id)
    return True


def _backfill_done_key(chat_id: str) -> str:
    return f"{BACKFILL_DONE_KEY_PREFIX}{chat_id}"


def _fetch_sync_pages(config: AppConfig, client: SyncClient, chat_id: str) -> list[dict[str, Any]]:
    first_page = client.fetch_messages_page(chat_id)
    all_items = list(first_page.items)

    with open_db(config.archive.path) as conn:
        backfill_done = get_runtime_state(conn, _backfill_done_key(chat_id)) == "1"

    if backfill_done or not first_page.has_more:
        return all_items

    cursor = first_page.oldest_cursor
    pages_left = max(0, int(config.beeper.history_backfill_pages) - 1)
    reached_start = not first_page.has_more
    while cursor and pages_left > 0:
        page = client.fetch_messages_page(chat_id, cursor=cursor, direction="before")
        # A page may legitimately yield no stored messages — on Matrix it can be
        # all reactions/receipts/state or events we lack keys for — while older
        # messages still sit further back. Keep paginating on the cursor, not on
        # whether this page had items.
        all_items.extend(page.items)
        pages_left -= 1
        if not page.has_more:
            reached_start = True
            break
        next_cursor = page.oldest_cursor
        if not next_cursor or next_cursor == cursor:
            break  # no forward progress; stop rather than loop forever
        cursor = next_cursor

    with open_db(config.archive.path) as conn:
        if reached_start:
            set_runtime_state(conn, _backfill_done_key(chat_id), "1")

    return all_items


def _update_sync_state(conn: sqlite3.Connection, chat_id: str, latest_sort_key: int | None) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO sync_state(chat_id, last_seen_sort_key, last_full_sync_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_seen_sort_key = excluded.last_seen_sort_key,
            last_full_sync_at = excluded.last_full_sync_at,
            updated_at = excluded.updated_at
        """,
        (chat_id, latest_sort_key, now, now),
    )


def sync_chat(config: AppConfig, client: SyncClient, chat_id: str) -> ChatSyncResult:
    init_db_path(config.archive.path)
    with open_db(config.archive.path) as conn:
        chat = conn.execute("SELECT name, is_allowed FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
    if chat is None or int(chat["is_allowed"]) != 1:
        return ChatSyncResult(chat_id, str(chat["name"]) if chat else chat_id, 0, 0, None)

    chat_payload = client.fetch_chat(chat_id)
    chat_name = _chat_name(chat_payload, chat_id)
    messages = _fetch_sync_pages(config, client, chat_id)
    deduped: dict[str, dict[str, Any]] = {}
    for message in messages:
        sort_key = _sort_key(message)
        message_id = _message_id(chat_id, message, sort_key)
        deduped[message_id] = message
    ordered = sorted((msg for msg in deduped.values() if _sort_key(msg) is not None), key=lambda msg: int(msg["sortKey"]))
    latest_sort_key = int(ordered[-1]["sortKey"]) if ordered else None

    stored_messages = 0
    with open_db(config.archive.path) as conn:
        conn.execute("BEGIN")
        _upsert_chat(conn, chat_id, chat_name)
        for message in ordered:
            if _upsert_message(conn, chat_id, chat_name, message):
                stored_messages += 1
        _update_sync_state(conn, chat_id, latest_sort_key)
        conn.commit()

    return ChatSyncResult(
        chat_id=chat_id,
        chat_name=chat_name,
        fetched_messages=len(ordered),
        stored_messages=stored_messages,
        latest_sort_key=latest_sort_key,
    )


def sync_chats(config: AppConfig, client: SyncClient, chat_ids: list[str] | None = None) -> SyncResult:
    target_chat_ids = chat_ids or list(config.beeper.indexed_chat_ids)
    results: list[ChatSyncResult] = []
    for chat_id in target_chat_ids:
        try:
            results.append(sync_chat(config, client, chat_id))
        except Exception as exc:
            # A single stale/deleted/left room (common after bridge changes)
            # must not abort syncing the rest.
            print(f"sync: skipping chat {chat_id}: {exc}", file=sys.stderr)
    return SyncResult(chats=results)
