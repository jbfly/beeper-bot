from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .beeper_api import BeeperApiClient, make_message_client
from .catchup import CatchupError, catchup_summary, format_catchup_result
from .config import AppConfig, ConfigError, ControlChatConfig, resolved_control_chats
from .db import (
    fetch_unsent_outbound,
    get_runtime_state,
    init_db_path,
    latest_sync_timestamp,
    mark_outbound_sent,
    open_db,
    set_runtime_state,
    utc_now,
)
from .discovery import add_dynamic_indexed_chat_ids, effective_indexed_chat_ids, match_unindexed_chats
from .llm import LlmError, ask_archive, format_ask_response
from .memory import (
    apply_pending_update,
    clear_pending_update,
    latest_pending_update,
    load_memory_state,
    looks_like_confirmation,
    looks_like_rejection,
    maybe_refresh_control_summary,
    queue_proposed_action,
    recent_control_turns,
    record_control_turn,
)
from .retrieval import format_find_response, search_archive
from .sync import sync_chats
from .tracing import finish_trace, trace_context, trace_event


def log(message: str) -> None:
    print(f"[{utc_now()}] {message}", flush=True)


CONTROL_CURSOR_KEY = "control_chat_last_seen_sort_key"
DEFAULT_STALE_SECONDS = 30


def _control_cursor_key(chat_id: str) -> str:
    """Per-control-chat cursor key. Each purpose-scoped chat tracks its own
    last-seen sort key so they advance independently. (The pre-multi-chat build
    used a single global key; the main chat simply re-seeds to 'now' on first
    poll under the new key — no re-answering of old messages.)"""
    return f"{CONTROL_CURSOR_KEY}:{chat_id}"
HTML_TAG_RE = re.compile(r"<[^>]+>")

MD_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
MD_FULL_BOLD_RE = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
MD_INLINE_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
MD_BULLET_RE = re.compile(r"^(\s*)[*•-]\s+")


CONTINUATION_MARKER_ROOM = 12
TRUNCATION_NOTE = "\n[truncated]"


def _wrap_words(line: str, limit: int) -> list[str]:
    """Split one over-long line on spaces, hard-splitting any single token
    (e.g. a giant URL) that still exceeds the limit."""
    chunks: list[str] = []
    current = ""
    for word in line.split(" "):
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(word) > limit:
            chunks.append(word[:limit])
            word = word[limit:]
        current = word
    if current:
        chunks.append(current)
    return chunks


def _wrap_segment(segment: str, limit: int) -> list[str]:
    """Split one over-long paragraph, preferring line then word boundaries."""
    chunks: list[str] = []
    current = ""
    for line in segment.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(line) <= limit:
            current = line
            continue
        chunks.extend(_wrap_words(line, limit))
    if current:
        chunks.append(current)
    return chunks


def split_message(text: str, limit: int, max_parts: int = 6) -> list[str]:
    """Split a reply into <=limit-char messages at natural boundaries.

    Prefers blank-line (paragraph) breaks, then lines, then words, then a
    hard character split. Parts beyond max_parts are dropped and a
    truncation note is added. When more than one part results, each gets an
    '(i/n)' continuation marker.
    """
    text = text.rstrip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    effective = max(1, limit - CONTINUATION_MARKER_ROOM)
    parts: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            parts.append(current.rstrip())
        current = ""

    for block in re.split(r"\n\s*\n", text):
        block = block.rstrip()
        if not block:
            continue
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= effective:
            current = candidate
            continue
        flush()
        if len(block) <= effective:
            current = block
            continue
        parts.extend(_wrap_segment(block, effective))
    flush()

    if len(parts) > max_parts:
        parts = parts[:max_parts]
        keep = max(1, effective - len(TRUNCATION_NOTE))
        parts[-1] = parts[-1][:keep].rstrip() + TRUNCATION_NOTE

    total = len(parts)
    if total > 1:
        parts = [f"{part}\n({idx}/{total})" for idx, part in enumerate(parts, 1)]
    return parts


def format_reply_for_chat(text: str) -> str:
    """Convert model markdown into something readable in a plain-text chat:
    emoji section headers, '•' bullets, real blank lines between sections."""
    out: list[str] = []
    for line in text.splitlines():
        header = MD_HEADER_RE.match(line)
        if header is None:
            header = MD_FULL_BOLD_RE.match(line)
        if header:
            if out and out[-1].strip():
                out.append("")
            out.append(f"🔹 {header.group(1).strip()}")
            continue
        line = MD_BULLET_RE.sub(lambda m: f"{m.group(1)}• ", line)
        line = MD_INLINE_BOLD_RE.sub(r"\1", line)
        out.append(line)
    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


class BridgeApiClient(Protocol):
    def fetch_chat(self, chat_id: str) -> dict: ...
    def fetch_messages(self, chat_id: str) -> list[dict]: ...
    def send_message(self, chat_id: str, text: str) -> None: ...


@dataclass(slots=True)
class RemoteCommand:
    mode: str
    text: str = ""


@dataclass(slots=True)
class BridgeLoopResult:
    processed_messages: int
    replied_messages: int
    busy_messages: int


class ControlBridge:
    def __init__(self, config: AppConfig, api_client: BridgeApiClient | None = None):
        self.config = config
        self.api_client = api_client or make_message_client(config.beeper)
        self.busy = False
        self._chat_listing: list[dict] = []
        self._chat_listing_at: float = 0.0
        self.control_chats = resolved_control_chats(config)
        # The control chat currently being processed. Set at the top of each
        # per-chat pass so _reply / cursor / persona all resolve against it.
        self._active: ControlChatConfig | None = None
        names = ", ".join(f"{c.name}={c.chat_id}" for c in self.control_chats) or "unset"
        log(f"bridge init control_chats=[{names}] indexed_chats={len(config.beeper.indexed_chat_ids)}")

    @property
    def control_chat_id(self) -> str:
        if self._active is not None:
            return self._active.chat_id
        if not self.control_chats:
            raise ConfigError(
                "A control chat is required for serve mode: set beeper.control_chat_id "
                "or define at least one [control_chats.*]"
            )
        return self.control_chats[0].chat_id

    def _message_text(self, message: dict) -> str:
        raw = str(message.get("text", "") or "")
        text = html.unescape(raw)
        text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        text = text.replace("</p>", "\n").replace("<p>", "")
        text = HTML_TAG_RE.sub("", text)
        return text.strip()

    def parse_command(self, text: str) -> RemoteCommand:
        stripped = text.strip()
        if not stripped:
            return RemoteCommand("ignore")
        if stripped.startswith(self.config.bridge.reply_prefix):
            return RemoteCommand("ignore")
        if stripped == "/help":
            return RemoteCommand("help")
        if stripped == "/status":
            return RemoteCommand("status")
        if stripped == "/reindex":
            return RemoteCommand("reindex")
        if stripped.startswith("/find "):
            return RemoteCommand("find", stripped[len("/find "):].strip())
        if stripped.startswith("/ask "):
            return RemoteCommand("ask", stripped[len("/ask "):].strip())
        if stripped.startswith("/index "):
            return RemoteCommand("index", stripped[len("/index "):].strip())
        if stripped.startswith("/catchup "):
            return RemoteCommand("catchup", stripped[len("/catchup "):].strip())
        if stripped == "/music-status":
            return RemoteCommand("music-status")
        if stripped == "/music" or stripped.startswith("/music "):
            return RemoteCommand("music", stripped[len("/music"):].strip())
        return RemoteCommand("ask", stripped)

    def _latest_sort_key(self, messages: list[dict]) -> int | None:
        values = [int(msg["sortKey"]) for msg in messages if msg.get("sortKey") not in (None, "")]
        return max(values) if values else None

    def _fresh_messages(self, messages: list[dict]) -> list[dict]:
        latest = self._latest_sort_key(messages)
        if latest is None:
            return []

        cursor_key = _control_cursor_key(self.control_chat_id)
        with open_db(self.config.archive.path) as conn:
            cursor = get_runtime_state(conn, cursor_key)
            if cursor is None:
                set_runtime_state(conn, cursor_key, str(latest))
                return []
            last_seen = int(cursor)

        fresh = [msg for msg in messages if msg.get("sortKey") not in (None, "") and int(msg["sortKey"]) > last_seen]
        fresh.sort(key=lambda msg: int(msg["sortKey"]))
        return fresh

    def _store_cursor(self, sort_key: int) -> None:
        with open_db(self.config.archive.path) as conn:
            set_runtime_state(conn, _control_cursor_key(self.control_chat_id), str(sort_key))

    def _sync_is_stale(self) -> bool:
        with open_db(self.config.archive.path) as conn:
            stamp = latest_sync_timestamp(conn)
        if stamp is None:
            return True
        try:
            then = datetime.fromisoformat(stamp)
        except ValueError:
            return True
        now = datetime.now(timezone.utc)
        return (now - then) > timedelta(seconds=DEFAULT_STALE_SECONDS)

    def _quick_sync_chat_ids(self) -> list[str] | None:
        """Allowlisted chats that were synced before and have newer activity
        per the live chat listing. Returns None when no listing is available
        (caller should fall back to a full sync)."""
        listing = self._all_chats()
        if not listing:
            return None
        allowed = set(effective_indexed_chat_ids(self.config))
        with open_db(self.config.archive.path) as conn:
            rows = conn.execute("SELECT chat_id, last_synced_at FROM chats").fetchall()
        synced_at = {str(row["chat_id"]): str(row["last_synced_at"] or "") for row in rows}
        candidates: list[tuple[str, str]] = []
        for chat in listing:
            chat_id = str(chat.get("id") or "")
            if chat_id not in allowed or chat_id not in synced_at:
                continue
            activity = str(chat.get("lastActivity") or "")
            if activity and activity > synced_at[chat_id]:
                candidates.append((activity, chat_id))
        candidates.sort(reverse=True)
        return [chat_id for _, chat_id in candidates[:10]]

    def _maybe_sync(self, force: bool = False, quick: bool = False) -> None:
        if not force and not self._sync_is_stale():
            return
        if quick and not force:
            # Keep the ask path fast: only refresh already-known chats with
            # new activity. First-time backfills belong to the periodic full
            # sync in the serve loop.
            chat_ids = self._quick_sync_chat_ids()
            if chat_ids is not None:
                if not chat_ids:
                    return
                log(f"quick sync chats={len(chat_ids)}")
                result = sync_chats(self.config, self.api_client, chat_ids=chat_ids)
                log(f"quick sync done fetched={result.total_fetched_messages} stored={result.total_stored_messages}")
                return
        all_chats = self._all_chats() if self.config.beeper.auto_index_recent_days > 0 else None
        chat_ids = effective_indexed_chat_ids(self.config, all_chats)
        log(f"sync start force={int(force)} chats={len(chat_ids)}")
        result = sync_chats(self.config, self.api_client, chat_ids=chat_ids)
        log(f"sync done fetched={result.total_fetched_messages} stored={result.total_stored_messages}")

    def _all_chats(self) -> list[dict]:
        if self._chat_listing and (time.monotonic() - self._chat_listing_at) < 600:
            return self._chat_listing
        fetch = getattr(self.api_client, "fetch_all_chats", None)
        if fetch is None:
            return []
        try:
            self._chat_listing = fetch()
            self._chat_listing_at = time.monotonic()
        except Exception as exc:
            log(f"chat listing failed: {exc}")
            return []
        return self._chat_listing

    def _sync_chats_on_demand(self, chats: list[dict]) -> list[str]:
        chat_ids = [str(chat.get("id") or "").strip() for chat in chats]
        chat_ids = [chat_id for chat_id in chat_ids if chat_id]
        if not chat_ids:
            return []
        add_dynamic_indexed_chat_ids(self.config, chat_ids)
        sync_chats(self.config, self.api_client, chat_ids=chat_ids)
        return [str(chat.get("title") or chat_id) for chat, chat_id in zip(chats, chat_ids)]

    def _maybe_index_for_question(self, question: str) -> list[str]:
        all_chats = self._all_chats()
        if not all_chats:
            return []
        indexed = set(effective_indexed_chat_ids(self.config))
        matches = match_unindexed_chats(question, all_chats, indexed)
        if not matches:
            return []
        names = self._sync_chats_on_demand(matches)
        log(f"on-demand index: {', '.join(names)}")
        trace_event("index.on_demand", {"question": question, "chats": names})
        return names

    def _help_text(self) -> str:
        return (
            f"{self.config.bridge.reply_prefix}Commands: plain text or /ask <question> = answer from local archive, "
            f"/find <query> = search archive, /catchup <chat-or-set> = digest of a chat or configured chat set since last catch-up, "
            f"/index <chat> = add a Beeper chat to the archive, /music <issue> = log a music-library issue (tags what's playing now), "
            f"/music-status = fixer queue summary, /status = runtime status, /reindex = force sync, /help = this help."
        )

    def _status_text(self) -> str:
        indexed = len(self.config.beeper.indexed_chat_ids)
        chats_note = f"control_chat={self.control_chat_id}"
        if len(self.control_chats) > 1:
            chats_note += f" (control_chats={len(self.control_chats)}: {', '.join(c.name for c in self.control_chats)})"
        return (
            f"{self.config.bridge.reply_prefix}Bridge ready. {chats_note}, "
            f"indexed_chats={indexed}, archive={self.config.archive.path}, llm={self.config.llm.model}"
        )

    def _reply(self, text: str) -> str:
        text = format_reply_for_chat(text)
        parts = split_message(text, self.config.bridge.max_reply_chars, self.config.bridge.max_reply_parts)
        for part in parts:
            self.api_client.send_message(self.control_chat_id, part)
        log(f"reply sent parts={len(parts)} chars={sum(len(p) for p in parts)}")
        return text

    def _handle_music(self, text: str) -> str:
        """Capture a music-library issue into the fixer queue, snapshotting now-playing.

        Delegates to music-library-project/scripts/fixer_capture.py so the capture
        logic (Navidrome getNowPlaying at capture time + queue append) lives with
        the library tooling, not the bot.
        """
        if not text:
            return self._reply(
                f"{self.config.bridge.reply_prefix}usage: /music <what's wrong or wanted> "
                "- captures it together with what's playing right now."
            )
        import subprocess
        try:
            proc = subprocess.run(
                [self.config.music.host_python,
                 str(self.config.music.project_root / "scripts" / "fixer_capture.py"), text],
                capture_output=True, text=True, timeout=45,
            )
            msg = (proc.stdout or proc.stderr or "").strip() or f"capture exited rc={proc.returncode}"
        except Exception as exc:
            msg = f"capture failed: {exc}"
        return self._reply(f"{self.config.bridge.reply_prefix}{msg}")

    def _handle_music_status(self) -> str:
        """Chat-friendly fixer-queue summary (open issues, pending questions, last resolutions)."""
        import subprocess
        script = str(self.config.music.project_root / "scripts" / "fixer_capture.py")
        try:
            proc = subprocess.run(
                [self.config.music.host_python, script, "--status"],
                capture_output=True, text=True, timeout=15,
            )
            msg = (proc.stdout or proc.stderr or "").strip() or f"status exited rc={proc.returncode}"
        except Exception as exc:
            msg = f"status failed: {exc}"
        return self._reply(f"{self.config.bridge.reply_prefix}{msg}")

    def _handle_music_chat(self, text: str) -> str:
        """Free text in the music chat: the cloud-LLM tool loop over the library.

        Falls back to command-only guidance when the cloud tier is unavailable —
        never routes music tool loops to the local model (no tool support there).
        """
        from .music import music_chat_turn
        turns = recent_control_turns(self.config, limit=self.config.music.history_turns, chat_id=self.control_chat_id)
        try:
            reply = music_chat_turn(self.config, text, turns=turns)
        except LlmError as exc:
            log(f"music chat llm error: {exc}")
            return self._reply(
                f"{self.config.bridge.reply_prefix}The music brain is offline ({exc.__class__.__name__}). "
                "/music <text> still captures issues and /music-status still works."
            )
        return self._reply(f"{self.config.bridge.reply_prefix}{reply}")

    def _handle_command(self, command: RemoteCommand) -> str | None:
        if command.mode == "ignore":
            return None
        if command.mode == "help":
            return self._reply(self._help_text())
        if command.mode == "status":
            return self._reply(self._status_text())
        if command.mode == "reindex":
            self._reply(f"{self.config.bridge.reply_prefix}Syncing now...")
            self._maybe_sync(force=True)
            return self._reply(f"{self.config.bridge.reply_prefix}Sync complete.")
        if command.mode == "find":
            self._maybe_sync(quick=True)
            return self._reply(f"{self.config.bridge.reply_prefix}{format_find_response(search_archive(self.config, command.text))}")
        if command.mode == "index":
            all_chats = self._all_chats()
            query = command.text.casefold()
            matches = [
                chat for chat in all_chats
                if query and query in str(chat.get("title") or "").casefold()
            ][:5]
            if not matches:
                return self._reply(f"{self.config.bridge.reply_prefix}No Beeper chat title matches '{command.text}'.")
            names = self._sync_chats_on_demand(matches)
            return self._reply(f"{self.config.bridge.reply_prefix}Indexed and synced: {', '.join(names)}.")
        if command.mode == "catchup":
            self._maybe_sync(quick=True)
            try:
                result = catchup_summary(self.config, command.text)
            except CatchupError as exc:
                return self._reply(f"{self.config.bridge.reply_prefix}{exc}")
            except LlmError as exc:
                if "connection refused" in str(exc).lower():
                    return self._reply(f"{self.config.bridge.reply_prefix}The local model is starting up. Try again in a moment.")
                raise
            return self._reply(f"{self.config.bridge.reply_prefix}{format_catchup_result(result)}")
        if command.mode == "ask":
            # The music chat's free text is a different animal: a cloud tool
            # loop over the music library, not archive Q&A.
            if self._active is not None and self._active.name == "music":
                return self._handle_music_chat(command.text)
            pending = latest_pending_update(self.config)
            if pending and looks_like_confirmation(command.text):
                return self._reply(f"{self.config.bridge.reply_prefix}{apply_pending_update(self.config, pending)}")
            if pending and looks_like_rejection(command.text):
                clear_pending_update(self.config, pending.update_id, status="cancelled")
                return self._reply(f"{self.config.bridge.reply_prefix}Okay. I did not save that memory update.")
            if pending:
                # A confirmation only applies to the immediately preceding
                # proposal; any other message retires it.
                clear_pending_update(self.config, pending.update_id, status="superseded")

            self._maybe_sync(quick=True)
            indexed_names = self._maybe_index_for_question(command.text)
            persona = self._active.persona if self._active is not None else ""
            try:
                response = ask_archive(
                    self.config,
                    command.text,
                    control_turns=recent_control_turns(self.config, limit=8, chat_id=self.control_chat_id),
                    memory_state=load_memory_state(self.config),
                    persona=persona,
                )
            except LlmError as exc:
                lowered = str(exc).lower()
                if "connection refused" in lowered:
                    return self._reply(f"{self.config.bridge.reply_prefix}The local model is starting up. Try again in a moment.")
                raise
            rendered = format_ask_response(response)
            if indexed_names:
                rendered = f"(Synced new chat{'s' if len(indexed_names) > 1 else ''} on the fly: {', '.join(indexed_names)})\n{rendered}"
            if response.proposed_action:
                queue_proposed_action(self.config, response.proposed_action)
            return self._reply(f"{self.config.bridge.reply_prefix}{rendered}")
        if command.mode == "music":
            return self._handle_music(command.text)
        if command.mode == "music-status":
            return self._handle_music_status()
        raise RuntimeError(f"Unknown command mode: {command.mode}")

    def _command_allowed(self, chat: ControlChatConfig, mode: str) -> bool:
        """A chat with a non-empty allowed_commands list only honors those modes
        (plus the always-safe help/status). Empty list = all commands."""
        if not chat.allowed_commands:
            return True
        return mode in chat.allowed_commands or mode in ("help", "status")

    def process_once(self) -> BridgeLoopResult:
        init_db_path(self.config.archive.path)
        self._drain_outbound()

        total = BridgeLoopResult(0, 0, 0)
        replied_any = False
        for chat in self.control_chats:
            self._active = chat
            try:
                result = self._process_control_chat(chat)
            finally:
                self._active = None
            total = BridgeLoopResult(
                total.processed_messages + result.processed_messages,
                total.replied_messages + result.replied_messages,
                total.busy_messages + result.busy_messages,
            )
            if result.replied_messages:
                replied_any = True

        if replied_any:
            try:
                summary = maybe_refresh_control_summary(self.config)
                if summary:
                    log(f"control summary refreshed chars={len(summary)}")
            except Exception as exc:
                log(f"control summary refresh failed: {exc}")

        return total

    def _process_control_chat(self, chat: ControlChatConfig) -> BridgeLoopResult:
        messages = self.api_client.fetch_messages(chat.chat_id)
        fresh = self._fresh_messages(messages)
        if not fresh:
            return BridgeLoopResult(0, 0, 0)

        log(f"poll chat={chat.name} fresh_messages={len(fresh)}")

        processed = 0
        replied = 0
        busy_messages = 0
        for message in fresh:
            processed += 1
            sort_key = int(message["sortKey"])
            text = self._message_text(message)
            if message.get("type") != "TEXT":
                self._store_cursor(sort_key)
                continue
            command = self.parse_command(text)
            if command.mode == "ignore":
                self._store_cursor(sort_key)
                continue
            if not self._command_allowed(chat, command.mode):
                # A purpose-scoped chat silently ignores commands outside its
                # allowlist (keeps feed-style chats quiet); no reply.
                log(f"message not-allowed chat={chat.name} mode={command.mode}")
                self._store_cursor(sort_key)
                continue
            if self.busy:
                log(f"message busy sort_key={sort_key} mode={command.mode}")
                self._reply(f"{self.config.bridge.reply_prefix}Busy. Try again in a moment.")
                busy_messages += 1
                self._store_cursor(sort_key)
                continue

            self.busy = True
            log(f"message handle chat={chat.name} sort_key={sort_key} mode={command.mode}")
            try:
                with trace_context(self.config, command.mode, question=text, source="beeper-control") as trace:
                    trace_event("bridge.message", {
                        "sort_key": sort_key,
                        "mode": command.mode,
                        "chat": chat.name,
                        "message_id": str(message.get("id") or ""),
                        "text": text,
                    })
                    record_control_turn(
                        self.config,
                        "user",
                        text,
                        chat_id=self.control_chat_id,
                        message_id=str(message.get("id") or ""),
                        sort_key=sort_key,
                    )
                    reply_text = self._handle_command(command)
                    if reply_text is not None:
                        replied += 1
                        record_control_turn(self.config, "assistant", reply_text, chat_id=self.control_chat_id)
                        trace_event("bridge.reply", {"text": reply_text})
                        finish_trace(trace, status="ok", final_answer=reply_text)
            except Exception as exc:
                log(f"message error sort_key={sort_key} mode={command.mode} error={exc}")
                self._reply(f"{self.config.bridge.reply_prefix}Error: {exc}")
                replied += 1
            finally:
                self.busy = False
                self._store_cursor(sort_key)

        return BridgeLoopResult(processed, replied, busy_messages)

    def _drain_outbound(self) -> None:
        """Deliver queued `beeper-bot notify` messages to their target control
        chats via the already-open transport. Unresolvable targets are dropped
        (marked sent); transient send failures are left queued for retry."""
        with open_db(self.config.archive.path) as conn:
            pending = fetch_unsent_outbound(conn, limit=20)
        if not pending:
            return
        by_name = {c.name: c.chat_id for c in self.control_chats}
        known_ids = {c.chat_id for c in self.control_chats}
        for row in pending:
            target = str(row["target"]).strip() or "main"
            chat_id = by_name.get(target)
            if chat_id is None:
                # Allow addressing a raw chat id directly; otherwise fall back to
                # the first control chat so a notification is never silently lost.
                chat_id = target if target in known_ids else (self.control_chats[0].chat_id if self.control_chats else None)
            if not chat_id:
                with open_db(self.config.archive.path) as conn:
                    mark_outbound_sent(conn, row["id"])
                continue
            try:
                for part in split_message(str(row["text"]), self.config.bridge.max_reply_chars, self.config.bridge.max_reply_parts):
                    self.api_client.send_message(chat_id, part)
            except Exception as exc:
                log(f"outbound deliver failed id={row['id']} target={target}: {exc}")
                continue  # leave queued; retry next cycle
            with open_db(self.config.archive.path) as conn:
                mark_outbound_sent(conn, row["id"])
            log(f"outbound delivered id={row['id']} target={target}")

    def _auto_derive_media(self) -> None:
        """Transcribe/describe a few new attachments per sync cycle so media
        becomes searchable without manual index-media runs."""
        from .media import run_derivation_pass

        per_cycle = max(1, int(self.config.media.auto_derive_per_cycle))
        for kind in ("voice-memo", "image"):
            results = run_derivation_pass(self.config, kind, limit=per_cycle)
            if results:
                done = sum(1 for item in results if item.status == "done")
                log(f"auto-derive {kind}: processed={len(results)} done={done}")
            if any(item.status == "failed" for item in results):
                # Likely the model endpoint is unavailable (GPU owned by
                # something else); stop this cycle instead of burning retries.
                break

    def serve_forever(self) -> None:
        poll_seconds = max(1, int(self.config.beeper.poll_seconds))
        sync_interval = max(poll_seconds, int(self.config.beeper.sync_interval_seconds))
        log(f"serve start poll_seconds={poll_seconds} sync_interval_seconds={sync_interval}")
        last_full_sync = 0.0
        while True:
            try:
                if time.monotonic() - last_full_sync >= sync_interval:
                    self._maybe_sync(force=True)
                    if self.config.media.auto_derive:
                        self._auto_derive_media()
                    last_full_sync = time.monotonic()
                self.process_once()
            except Exception as exc:
                log(f"serve loop error={exc}")
            time.sleep(poll_seconds)
