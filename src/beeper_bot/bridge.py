from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .beeper_api import BeeperApiClient
from .catchup import CatchupError, catchup_summary, format_catchup_result
from .config import AppConfig, ConfigError
from .db import get_runtime_state, init_db_path, latest_sync_timestamp, open_db, set_runtime_state, utc_now
from .discovery import add_dynamic_indexed_chat_ids, effective_indexed_chat_ids, match_unindexed_chats
from .llm import LlmError, ask_archive, format_ask_response
from .memory import (
    apply_pending_update,
    clear_pending_update,
    latest_pending_update,
    load_memory_state,
    looks_like_confirmation,
    looks_like_rejection,
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
HTML_TAG_RE = re.compile(r"<[^>]+>")


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
        self.api_client = api_client or BeeperApiClient(config.beeper)
        self.busy = False
        self._chat_listing: list[dict] = []
        self._chat_listing_at: float = 0.0
        log(f"bridge init control_chat={config.beeper.control_chat_id or 'unset'} indexed_chats={len(config.beeper.indexed_chat_ids)}")

    @property
    def control_chat_id(self) -> str:
        chat_id = self.config.beeper.control_chat_id.strip()
        if not chat_id:
            raise ConfigError("beeper.control_chat_id is required for serve mode")
        return chat_id

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
        return RemoteCommand("ask", stripped)

    def _latest_sort_key(self, messages: list[dict]) -> int | None:
        values = [int(msg["sortKey"]) for msg in messages if msg.get("sortKey") not in (None, "")]
        return max(values) if values else None

    def _fresh_messages(self, messages: list[dict]) -> list[dict]:
        latest = self._latest_sort_key(messages)
        if latest is None:
            return []

        with open_db(self.config.archive.path) as conn:
            cursor = get_runtime_state(conn, CONTROL_CURSOR_KEY)
            if cursor is None:
                set_runtime_state(conn, CONTROL_CURSOR_KEY, str(latest))
                return []
            last_seen = int(cursor)

        fresh = [msg for msg in messages if msg.get("sortKey") not in (None, "") and int(msg["sortKey"]) > last_seen]
        fresh.sort(key=lambda msg: int(msg["sortKey"]))
        return fresh

    def _store_cursor(self, sort_key: int) -> None:
        with open_db(self.config.archive.path) as conn:
            set_runtime_state(conn, CONTROL_CURSOR_KEY, str(sort_key))

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

    def _maybe_sync(self, force: bool = False) -> None:
        if not force and not self._sync_is_stale():
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
            f"/find <query> = search archive, /catchup <chat> = digest of a chat since last catch-up, "
            f"/index <chat> = add a Beeper chat to the archive, /status = runtime status, /reindex = force sync, /help = this help."
        )

    def _status_text(self) -> str:
        indexed = len(self.config.beeper.indexed_chat_ids)
        return (
            f"{self.config.bridge.reply_prefix}Bridge ready. control_chat={self.control_chat_id}, "
            f"indexed_chats={indexed}, archive={self.config.archive.path}, llm={self.config.llm.model}"
        )

    def _reply(self, text: str) -> str:
        payload = text[: self.config.bridge.max_reply_chars].rstrip()
        self.api_client.send_message(self.control_chat_id, payload)
        log(f"reply sent chars={len(payload)}")
        return payload

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
            self._maybe_sync()
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
            self._maybe_sync()
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

            self._maybe_sync()
            indexed_names = self._maybe_index_for_question(command.text)
            try:
                response = ask_archive(
                    self.config,
                    command.text,
                    control_turns=recent_control_turns(self.config, limit=8),
                    memory_state=load_memory_state(self.config),
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
        raise RuntimeError(f"Unknown command mode: {command.mode}")

    def process_once(self) -> BridgeLoopResult:
        init_db_path(self.config.archive.path)
        messages = self.api_client.fetch_messages(self.control_chat_id)
        fresh = self._fresh_messages(messages)
        if not fresh:
            return BridgeLoopResult(0, 0, 0)

        log(f"poll fresh_messages={len(fresh)}")

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
            if self.busy:
                log(f"message busy sort_key={sort_key} mode={command.mode}")
                self._reply(f"{self.config.bridge.reply_prefix}Busy. Try again in a moment.")
                busy_messages += 1
                self._store_cursor(sort_key)
                continue

            self.busy = True
            log(f"message handle sort_key={sort_key} mode={command.mode}")
            try:
                with trace_context(self.config, command.mode, question=text, source="beeper-control") as trace:
                    trace_event("bridge.message", {
                        "sort_key": sort_key,
                        "mode": command.mode,
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

    def serve_forever(self) -> None:
        poll_seconds = max(1, int(self.config.beeper.poll_seconds))
        log(f"serve start poll_seconds={poll_seconds}")
        while True:
            try:
                self.process_once()
            except Exception as exc:
                log(f"serve loop error={exc}")
            time.sleep(poll_seconds)
