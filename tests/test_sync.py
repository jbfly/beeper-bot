from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import MessagePage
from beeper_bot.config import load_config
from beeper_bot.db import open_db
from beeper_bot.offline_archive import approve_chat, revoke_chat
from beeper_bot.retrieval import search_archive
from beeper_bot.sync import normalize_text, sync_chat, sync_chats


class FakeBeeperClient:
    def __init__(self, chats: dict[str, dict], messages: dict[str, list[dict]], pages: dict[str, list[list[dict]]] | None = None):
        self.chats = chats
        self.messages = messages
        self.pages = pages or {chat_id: [list(items)] for chat_id, items in messages.items()}

    def fetch_chat(self, chat_id: str) -> dict:
        return self.chats[chat_id]

    def fetch_messages(self, chat_id: str) -> list[dict]:
        return list(self.messages[chat_id])

    def fetch_messages_page(self, chat_id: str, cursor: str | None = None, direction: str | None = None) -> MessagePage:
        pages = self.pages[chat_id]
        if cursor is None:
            idx = 0
        else:
            idx = int(cursor)
        items = list(pages[idx])
        next_idx = idx + 1
        has_more = next_idx < len(pages)
        oldest_cursor = str(next_idx) if has_more else None
        newest_cursor = str(idx)
        return MessagePage(items=items, has_more=has_more, oldest_cursor=oldest_cursor, newest_cursor=newest_cursor)


class SyncTest(unittest.TestCase):
    def _write_config(self, tmpdir: str, indexed_chat_ids: list[str]) -> Path:
        config_path = Path(tmpdir) / "config.toml"
        db_path = Path(tmpdir) / "archive.sqlite3"
        config_path.write_text(
            "\n".join(
                [
                    "[archive]",
                    f'path = "{db_path}"',
                    "",
                    "[beeper]",
                    f"indexed_chat_ids = [{', '.join(f'\"{chat_id}\"' for chat_id in indexed_chat_ids)}]",
                ]
            )
        )
        return config_path

    def test_normalize_text(self) -> None:
        self.assertEqual(normalize_text("  hi\r\nthere\t\tfriend  "), "hi\nthere friend")

    def test_sync_chat_skips_unapproved_and_unknown_chats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir, []))
            approve_chat(config, "chat-denied", "Denied chat")
            revoke_chat(config, "chat-denied")
            client = FakeBeeperClient(chats={}, messages={})

            denied = sync_chat(config, client, "chat-denied")
            unknown = sync_chat(config, client, "chat-unknown")

            self.assertEqual((denied.fetched_messages, denied.stored_messages), (0, 0))
            self.assertEqual((unknown.fetched_messages, unknown.stored_messages), (0, 0))
            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0], 0)
                self.assertIsNone(conn.execute("SELECT 1 FROM chats WHERE chat_id = ?", ("chat-unknown",)).fetchone())

    def test_sync_chat_inserts_rows_and_fts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir, ["chat-a"]))
            approve_chat(config, "chat-a", "Family logistics")
            client = FakeBeeperClient(
                chats={"chat-a": {"title": "Family logistics"}},
                messages={
                    "chat-a": [
                        {
                            "id": "msg-2",
                            "sortKey": "2",
                            "timestamp": "2026-06-01T12:05:00Z",
                            "senderID": "u2",
                            "senderName": "Seth",
                            "type": "TEXT",
                            "text": "Meet at 123 Sample St",
                        },
                        {
                            "id": "msg-1",
                            "sortKey": "1",
                            "timestamp": "2026-06-01T12:00:00Z",
                            "senderID": "u1",
                            "senderName": "Julie",
                            "type": "TEXT",
                            "text": "Okay",
                        },
                    ]
                },
            )

            result = sync_chats(config, client)
            self.assertEqual(result.total_fetched_messages, 2)
            self.assertEqual(result.total_stored_messages, 2)
            self.assertEqual(result.chats[0].chat_name, "Family logistics")
            self.assertEqual(result.chats[0].latest_sort_key, 2)

            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT last_seen_sort_key FROM sync_state WHERE chat_id = ?", ("chat-a",)).fetchone()[0], 2)
                row = conn.execute(
                    "SELECT name, last_synced_at, is_allowed, approval_source, approved_at FROM chats WHERE chat_id = ?",
                    ("chat-a",),
                ).fetchone()
                self.assertEqual(row[0], "Family logistics")
                self.assertIsNotNone(row[1])
                self.assertEqual(tuple(row[2:4]), (1, "operator"))
                self.assertIsNotNone(row[4])
            self.assertEqual(len(search_archive(config, "123 Sample St").results), 1)

    def test_sync_chat_upserts_edited_message_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir, ["chat-a"]))
            approve_chat(config, "chat-a", "Family logistics")
            client = FakeBeeperClient(
                chats={"chat-a": {"title": "Family logistics"}},
                messages={
                    "chat-a": [
                        {
                            "id": "msg-1",
                            "sortKey": "1",
                            "timestamp": "2026-06-01T12:00:00Z",
                            "senderID": "u1",
                            "senderName": "Julie",
                            "type": "TEXT",
                            "text": "Old text",
                        }
                    ]
                },
            )
            sync_chats(config, client)

            client.messages["chat-a"][0]["text"] = "New text"
            client.pages["chat-a"] = [client.messages["chat-a"]]
            result = sync_chats(config, client)
            self.assertEqual(result.total_stored_messages, 1)

            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0], 1)
                row = conn.execute(
                    "SELECT text, normalized_text FROM messages WHERE message_id = ?",
                    ("msg-1",),
                ).fetchone()
                self.assertEqual(row[0], "New text")
                self.assertEqual(row[1], "New text")
                fts_row = conn.execute(
                    "SELECT text FROM message_fts WHERE message_id = ?",
                    ("msg-1",),
                ).fetchone()
                self.assertEqual(fts_row[0], "New text")

    def test_sync_chat_backfills_multiple_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_config(tmpdir, ["chat-a"])
            config = load_config(config_path)
            approve_chat(config, "chat-a", "Family logistics")
            config.beeper.history_backfill_pages = 3
            client = FakeBeeperClient(
                chats={"chat-a": {"title": "Family logistics"}},
                messages={"chat-a": []},
                pages={
                    "chat-a": [
                        [
                            {"id": "msg-3", "sortKey": "3", "timestamp": "2026-06-01T12:02:00Z", "senderID": "u1", "senderName": "Julie", "type": "TEXT", "text": "three"},
                            {"id": "msg-2", "sortKey": "2", "timestamp": "2026-06-01T12:01:00Z", "senderID": "u1", "senderName": "Julie", "type": "TEXT", "text": "two"},
                        ],
                        [
                            {"id": "msg-1", "sortKey": "1", "timestamp": "2026-06-01T12:00:00Z", "senderID": "u1", "senderName": "Julie", "type": "TEXT", "text": "one"},
                        ],
                    ]
                },
            )
            result = sync_chats(config, client)
            self.assertEqual(result.total_fetched_messages, 3)
            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
