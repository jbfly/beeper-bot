from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import MessagePage
from beeper_bot.catchup import CatchupError, build_catchup_prompt, catchup_summary, resolve_chat
from beeper_bot.config import load_config
from beeper_bot.discovery import (
    add_dynamic_indexed_chat_ids,
    dynamic_indexed_chat_ids,
    effective_indexed_chat_ids,
    match_unindexed_chats,
    recent_chat_ids,
)
from beeper_bot.sync import sync_chats


class FakeBeeperClient:
    def __init__(self, chats: dict[str, dict], messages: dict[str, list[dict]]):
        self.chats = chats
        self.messages = messages

    def fetch_chat(self, chat_id: str) -> dict:
        return self.chats[chat_id]

    def fetch_messages(self, chat_id: str) -> list[dict]:
        return list(self.messages[chat_id])

    def fetch_messages_page(self, chat_id: str, cursor: str | None = None, direction: str | None = None) -> MessagePage:
        return MessagePage(items=list(self.messages[chat_id]), has_more=False, oldest_cursor=None, newest_cursor=None)


class FakeSummarizer:
    def __init__(self, summary: str = "Digest: planning a dinner on Friday."):
        self.summary = summary
        self.prompts: list[str] = []

    def summarize_text(self, config, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.summary


def _message(idx: int, sender: str, text: str) -> dict:
    return {
        "id": f"msg-{idx}",
        "sortKey": str(idx),
        "timestamp": f"2026-06-{idx:02d}T10:00:00Z",
        "senderID": f"u-{sender}",
        "senderName": sender,
        "type": "TEXT",
        "text": text,
    }


class CatchupTest(unittest.TestCase):
    def _config_with_group_chat(self):
        tmpdir = tempfile.TemporaryDirectory()
        config_path = Path(tmpdir.name) / "config.toml"
        db_path = Path(tmpdir.name) / "archive.sqlite3"
        config_path.write_text(
            "\n".join(
                [
                    "[archive]",
                    f'path = "{db_path}"',
                    "",
                    "[beeper]",
                    'indexed_chat_ids = ["chat-group"]',
                ]
            )
        )
        config = load_config(config_path)
        client = FakeBeeperClient(
            chats={"chat-group": {"title": "Bom Sucesso Community"}},
            messages={
                "chat-group": [
                    _message(1, "Maria", "The pool reopens on Saturday."),
                    _message(2, "Pedro", "Dinner at the clubhouse Friday 19:00, who is in?"),
                    _message(3, "Maria", "John, can you bring the speaker?"),
                ]
            },
        )
        sync_chats(config, client)
        return config, tmpdir

    def test_resolve_chat_matches_partial_title(self) -> None:
        config, tmpdir = self._config_with_group_chat()
        self.addCleanup(tmpdir.cleanup)
        chat_id, name = resolve_chat(config, "bom sucesso")
        self.assertEqual(chat_id, "chat-group")
        self.assertEqual(name, "Bom Sucesso Community")
        with self.assertRaises(CatchupError):
            resolve_chat(config, "nonexistent chat")

    def test_catchup_summarizes_new_messages_and_advances_cursor(self) -> None:
        config, tmpdir = self._config_with_group_chat()
        self.addCleanup(tmpdir.cleanup)
        summarizer = FakeSummarizer()
        result = catchup_summary(config, "bom sucesso", summarizer)
        self.assertEqual(result.message_count, 3)
        self.assertIn("Digest", result.summary)
        self.assertIn("pool reopens", summarizer.prompts[0])
        self.assertIn("bring the speaker", summarizer.prompts[0])

        second = catchup_summary(config, "bom sucesso", summarizer)
        self.assertEqual(second.message_count, 0)
        self.assertIn("No new messages", second.summary)

    def test_catchup_fixture_cursor_does_not_advance_state(self) -> None:
        config, tmpdir = self._config_with_group_chat()
        self.addCleanup(tmpdir.cleanup)
        summarizer = FakeSummarizer()
        result = catchup_summary(config, "bom sucesso", summarizer, since_sort_key=1, update_cursor=False)
        self.assertEqual(result.message_count, 2)
        again = catchup_summary(config, "bom sucesso", summarizer, since_sort_key=1, update_cursor=False)
        self.assertEqual(again.message_count, 2)

    def test_build_catchup_prompt_includes_senders_and_truncation_note(self) -> None:
        messages = [
            {"sender_name": "Maria", "timestamp": "2026-06-01T10:00:00Z", "text": "hello " * 100, "sort_key": 1},
        ]
        prompt = build_catchup_prompt("Test Chat", messages, truncated=True)
        self.assertIn("Maria", prompt)
        self.assertIn("most recent messages", prompt)
        self.assertIn("...", prompt)


class DiscoveryTest(unittest.TestCase):
    CHATS = [
        {"id": "c-old", "title": "College Friends", "lastActivity": "2024-01-01T00:00:00Z"},
        {"id": "c-new", "title": "Bom Sucesso Community", "lastActivity": "2026-06-11T00:00:00Z"},
        {"id": "c-num", "title": "(888) 593-1419", "lastActivity": "2026-06-11T00:00:00Z"},
        {"id": "c-arch", "title": "Archived Chat", "lastActivity": "2026-06-11T00:00:00Z", "isArchived": True},
        {"id": "c-elsa", "title": "Elsa", "lastActivity": "2026-06-10T00:00:00Z"},
    ]

    def test_recent_chat_ids_filters_noise_and_age(self) -> None:
        ids = recent_chat_ids(self.CHATS, days=36500, max_chats=10)
        self.assertIn("c-old", ids)
        self.assertNotIn("c-num", ids)
        self.assertNotIn("c-arch", ids)

    def test_match_unindexed_chats_by_title(self) -> None:
        matches = match_unindexed_chats(
            "What did people say in the Bom Sucesso community chat about the pool?",
            self.CHATS,
            indexed_ids=set(),
        )
        self.assertEqual(matches[0]["id"], "c-new")

    def test_match_requires_meaningful_overlap(self) -> None:
        matches = match_unindexed_chats(
            "What is the weather like today?",
            self.CHATS,
            indexed_ids=set(),
        )
        self.assertEqual(matches, [])

    def test_match_skips_already_indexed(self) -> None:
        matches = match_unindexed_chats(
            "Anything new in Bom Sucesso Community?",
            self.CHATS,
            indexed_ids={"c-new"},
        )
        self.assertEqual(matches, [])

    def test_dynamic_chat_ids_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            db_path = Path(tmpdir) / "archive.sqlite3"
            config_path.write_text(f'[archive]\npath = "{db_path}"\n\n[beeper]\nindexed_chat_ids = ["base-chat"]\n')
            config = load_config(config_path)
            from beeper_bot.db import init_db_path

            init_db_path(config.archive.path)
            self.assertEqual(dynamic_indexed_chat_ids(config), [])
            add_dynamic_indexed_chat_ids(config, ["chat-x", "chat-y"])
            add_dynamic_indexed_chat_ids(config, ["chat-y", "chat-z"])
            self.assertEqual(dynamic_indexed_chat_ids(config), ["chat-x", "chat-y", "chat-z"])
            self.assertEqual(
                effective_indexed_chat_ids(config),
                ["base-chat", "chat-x", "chat-y", "chat-z"],
            )


if __name__ == "__main__":
    unittest.main()
