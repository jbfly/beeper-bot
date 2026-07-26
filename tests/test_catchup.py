from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import MessagePage
from beeper_bot.catchup import (
    CatchupError,
    build_catchup_prompt,
    catchup_summary,
    parse_chat_digest_request,
    resolve_chat,
    resolve_chats,
)
from beeper_bot.config import load_config
from beeper_bot.offline_archive import approve_chat
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
            chats={"chat-group": {"title": "Neighborhood Community"}},
            messages={
                "chat-group": [
                    _message(1, "Maria", "The pool reopens on Saturday."),
                    _message(2, "Pedro", "Dinner at the clubhouse Friday 19:00, who is in?"),
                    _message(3, "Maria", "John, can you bring the speaker?"),
                ]
            },
        )
        for chat_id in client.chats:
            approve_chat(config, chat_id, chat_id)
        sync_chats(config, client)
        return config, tmpdir

    def test_resolve_chat_matches_partial_title(self) -> None:
        config, tmpdir = self._config_with_group_chat()
        self.addCleanup(tmpdir.cleanup)
        chat_id, name = resolve_chat(config, "neighborhood")
        self.assertEqual(chat_id, "chat-group")
        self.assertEqual(name, "Neighborhood Community")
        with self.assertRaises(CatchupError):
            resolve_chat(config, "nonexistent chat")

    def test_catchup_summarizes_new_messages_and_advances_cursor(self) -> None:
        config, tmpdir = self._config_with_group_chat()
        self.addCleanup(tmpdir.cleanup)
        summarizer = FakeSummarizer()
        result = catchup_summary(config, "neighborhood", summarizer)
        self.assertEqual(result.message_count, 3)
        self.assertIn("Digest", result.summary)
        self.assertIn("pool reopens", summarizer.prompts[0])
        self.assertIn("bring the speaker", summarizer.prompts[0])

        second = catchup_summary(config, "neighborhood", summarizer)
        self.assertEqual(second.message_count, 0)
        self.assertIn("No new messages", second.summary)

    def test_catchup_fixture_cursor_does_not_advance_state(self) -> None:
        config, tmpdir = self._config_with_group_chat()
        self.addCleanup(tmpdir.cleanup)
        summarizer = FakeSummarizer()
        result = catchup_summary(config, "neighborhood", summarizer, since_sort_key=1, update_cursor=False)
        self.assertEqual(result.message_count, 2)
        again = catchup_summary(config, "neighborhood", summarizer, since_sort_key=1, update_cursor=False)
        self.assertEqual(again.message_count, 2)

    def test_build_catchup_prompt_includes_senders_and_truncation_note(self) -> None:
        messages = [
            {"sender_name": "Maria", "timestamp": "2026-06-01T10:00:00Z", "text": "hello " * 100, "sort_key": 1},
        ]
        prompt = build_catchup_prompt("Test Chat", messages, truncated=True)
        self.assertIn("Maria", prompt)
        self.assertIn("most recent messages", prompt)
        self.assertIn("...", prompt)


class MultiChatCatchupTest(unittest.TestCase):
    def _config_with_festival_chats(self):
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
                    'indexed_chat_ids = ["chat-lineup", "chat-debates", "chat-rangutans"]',
                    "",
                    "[chat_sets.sample_festival]",
                    'display_name = "Sample Festival"',
                    'aliases = ["sample festival", "sample festival", "sample", "sample"]',
                    'chats = ["Sample Artists and Lineup", "Sample Logistics and Debates", "Sample Volunteers"]',
                ]
            )
        )
        config = load_config(config_path)
        client = FakeBeeperClient(
            chats={
                "chat-lineup": {"title": "Sample Artists and Lineup"},
                "chat-debates": {"title": "Sample Logistics and Debates"},
                "chat-rangutans": {"title": "Sample Volunteers"},
            },
            messages={
                "chat-lineup": [_message(1, "Maya", "Lineup drops Friday at noon.")],
                "chat-debates": [_message(2, "Rui", "The shuttle schedule changed to hourly.")],
                "chat-rangutans": [_message(3, "Pete", "Camp build starts June 20.")],
            },
        )
        for chat_id in client.chats:
            approve_chat(config, chat_id, chat_id)
        sync_chats(config, client)
        return config, tmpdir

    def test_resolve_chats_returns_all_matches_and_fuzzy_typos(self) -> None:
        config, tmpdir = self._config_with_festival_chats()
        self.addCleanup(tmpdir.cleanup)
        names = {name for _, name in resolve_chats(config, "sample")}
        self.assertEqual(len(names), 3)
        set_names = {name for _, name in resolve_chats(config, "sample festival")}
        self.assertEqual(set_names, names)
        # misspelling resolves via fuzzy matching
        chats = resolve_chats(config, "sample volunters")
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0][1], "Sample Volunteers")

    def test_multi_chat_digest_combines_sections_and_advances_cursors(self) -> None:
        config, tmpdir = self._config_with_festival_chats()
        self.addCleanup(tmpdir.cleanup)
        summarizer = FakeSummarizer("Digest with sections.")
        result = catchup_summary(config, "sample", summarizer)
        self.assertEqual(result.message_count, 3)
        self.assertIn("Sample Festival", result.chat_name)
        self.assertIn("3 chats", result.chat_name)
        prompt = summarizer.prompts[0]
        self.assertIn("## Sample Artists and Lineup", prompt)
        self.assertIn("## Sample Volunteers", prompt)
        self.assertIn("shuttle schedule", prompt)

        second = catchup_summary(config, "sample", summarizer)
        self.assertEqual(second.message_count, 0)
        self.assertIn("No new messages", second.summary)

    def test_parse_chat_digest_request_shapes(self) -> None:
        self.assertEqual(parse_chat_digest_request("Give me a summary of the sample volunters chat"), "sample volunters")
        self.assertEqual(parse_chat_digest_request("Summarize all of the neighborhood chats"), "neighborhood")
        self.assertEqual(parse_chat_digest_request("What's been happening in the Sample groups?"), "Sample")
        self.assertEqual(parse_chat_digest_request("What is happening in Neighborhood?"), "Neighborhood")
        self.assertEqual(parse_chat_digest_request("Summarize all chats related to Neighborhood"), "Neighborhood")
        self.assertEqual(parse_chat_digest_request("Catch me up on the Building Updates group chat"), "Building Updates")
        self.assertEqual(parse_chat_digest_request("Catch me up on sample festival"), "sample festival")
        # no digest intent or no chat noun -> not a digest request
        self.assertIsNone(parse_chat_digest_request("What did Anna say in the chat?"))
        self.assertIsNone(parse_chat_digest_request("Summarize the 21 min memo"))
        self.assertIsNone(parse_chat_digest_request("What address did Taylor send?"))

    def test_ask_routes_digest_requests_to_catchup(self) -> None:
        from beeper_bot.llm import ask_archive

        config, tmpdir = self._config_with_festival_chats()
        self.addCleanup(tmpdir.cleanup)
        summarizer = FakeSummarizer("Festival digest here.")
        response = ask_archive(
            config,
            "What is happening in sample festival?",
            llm_client=summarizer,
            control_turns=[],
            memory_state={},
        )
        self.assertEqual(response.answer_path, "model")
        self.assertIn("Festival digest here.", response.answer)
        self.assertIn("3 chats", response.answer)


class DiscoveryTest(unittest.TestCase):
    CHATS = [
        {"id": "c-old", "title": "College Friends", "lastActivity": "2024-01-01T00:00:00Z"},
        {"id": "c-new", "title": "Neighborhood Community", "lastActivity": "2026-06-11T00:00:00Z"},
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
            "What did people say in the Neighborhood community chat about the pool?",
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
            "Anything new in Neighborhood Community?",
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
