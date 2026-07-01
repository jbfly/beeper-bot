from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import MessagePage
from beeper_bot.bridge import ControlBridge
from beeper_bot.config import load_config
from beeper_bot.llm import LlmError


class FakeBeeperClient:
    def __init__(self, chats: dict[str, dict], messages: dict[str, list[dict]]):
        self.chats = chats
        self.messages = messages
        self.sent_messages: list[tuple[str, str]] = []

    def fetch_chat(self, chat_id: str) -> dict:
        return self.chats[chat_id]

    def fetch_messages(self, chat_id: str) -> list[dict]:
        return list(self.messages[chat_id])

    def fetch_messages_page(self, chat_id: str, cursor: str | None = None, direction: str | None = None) -> MessagePage:
        items = list(self.messages[chat_id])
        return MessagePage(items=items, has_more=False, oldest_cursor=None, newest_cursor=None)

    def send_message(self, chat_id: str, text: str) -> None:
        self.sent_messages.append((chat_id, text))


class RaisingAskClient(FakeBeeperClient):
    def __init__(self, chats: dict[str, dict], messages: dict[str, list[dict]]):
        super().__init__(chats, messages)


class BridgeTest(unittest.TestCase):
    def _write_config(self, tmpdir: str) -> Path:
        config_path = Path(tmpdir) / "config.toml"
        db_path = Path(tmpdir) / "archive.sqlite3"
        token_path = Path(tmpdir) / "token"
        token_path.write_text("dummy-token\n")
        config_path.write_text(
            "\n".join(
                [
                    "[beeper]",
                    'api_base = "http://localhost:23373/v1"',
                    f'token_file = "{token_path}"',
                    'control_chat_id = "control-chat"',
                    'indexed_chat_ids = ["indexed-chat"]',
                    "poll_seconds = 1",
                    "sync_interval_seconds = 300",
                    "history_fetch_limit = 500",
                    "http_timeout_seconds = 30",
                    "",
                    "[archive]",
                    f'path = "{db_path}"',
                    "",
                    "[llm]",
                    'base_url = "http://127.0.0.1:8090/v1"',
                    'model = "gemma4-vladimir-26b-local"',
                    "",
                    "[bridge]",
                    'reply_prefix = "[BEEPER-BOT] "',
                    "max_reply_chars = 3500",
                    "send_ack = true",
                ]
            )
        )
        return config_path

    def test_parse_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = ControlBridge(load_config(self._write_config(tmpdir)), api_client=FakeBeeperClient({}, {}))
            self.assertEqual(bridge.parse_command("/help").mode, "help")
            self.assertEqual(bridge.parse_command("/status").mode, "status")
            self.assertEqual(bridge.parse_command("/reindex").mode, "reindex")
            self.assertEqual(bridge.parse_command("/find hello").text, "hello")
            self.assertEqual(bridge.parse_command("hello").mode, "ask")
            self.assertEqual(bridge.parse_command("[BEEPER-BOT] hi").mode, "ignore")

    def test_process_once_help_then_find(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir))
            client = FakeBeeperClient(
                chats={
                    "control-chat": {"title": "Control"},
                    "indexed-chat": {"title": "Morgan Wright"},
                },
                messages={
                    "control-chat": [
                        {"id": "c1", "sortKey": "1", "timestamp": "2026-06-05T18:00:00Z", "senderName": "Operator", "type": "TEXT", "text": "/help"},
                    ],
                    "indexed-chat": [
                        {"id": "m1", "sortKey": "10", "timestamp": "2026-06-03T18:10:58.000Z", "senderID": "u1", "senderName": "Morgan Wright", "type": "TEXT", "text": "Ryzen 7 5800X3D AM4 10th Anniversary Edition"},
                    ],
                },
            )
            bridge = ControlBridge(config, api_client=client)

            first = bridge.process_once()
            self.assertEqual(first.processed_messages, 0)
            self.assertEqual(client.sent_messages, [])

            client.messages["control-chat"].append(
                {"id": "c2", "sortKey": "2", "timestamp": "2026-06-05T18:01:00Z", "senderName": "Operator", "type": "TEXT", "text": "/help"}
            )
            second = bridge.process_once()
            self.assertEqual(second.replied_messages, 1)
            self.assertIn("Commands:", client.sent_messages[-1][1])

            client.messages["control-chat"].append(
                {"id": "c3", "sortKey": "3", "timestamp": "2026-06-05T18:02:00Z", "senderName": "Operator", "type": "TEXT", "text": "/find 5800X3D"}
            )
            third = bridge.process_once()
            self.assertEqual(third.replied_messages, 1)
            self.assertIn("Top matches for: 5800X3D", client.sent_messages[-1][1])
            self.assertIn("Ryzen 7 5800X3D", client.sent_messages[-1][1])

    def test_process_once_ask_model_starting_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir))
            client = FakeBeeperClient(
                chats={
                    "control-chat": {"title": "Control"},
                    "indexed-chat": {"title": "Morgan Wright"},
                },
                messages={
                    "control-chat": [
                        {"id": "c1", "sortKey": "1", "timestamp": "2026-06-05T18:00:00Z", "senderName": "Operator", "type": "TEXT", "text": "bootstrap"},
                    ],
                    "indexed-chat": [],
                },
            )
            bridge = ControlBridge(config, api_client=client)
            bridge.process_once()
            client.messages["control-chat"].append(
                {"id": "c2", "sortKey": "2", "timestamp": "2026-06-05T18:01:00Z", "senderName": "Operator", "type": "TEXT", "text": "What did Thomas send?"}
            )

            import beeper_bot.bridge as bridge_mod
            original = bridge_mod.ask_archive
            def raising_ask_archive(config, question, **kwargs):
                raise LlmError("LLM API failed: <urlopen error [Errno 111] Connection refused>")
            bridge_mod.ask_archive = raising_ask_archive
            try:
                result = bridge.process_once()
            finally:
                bridge_mod.ask_archive = original
            self.assertEqual(result.replied_messages, 1)
            self.assertIn("local model is starting up", client.sent_messages[-1][1].lower())

    def test_process_once_can_confirm_pending_alias_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir))
            client = FakeBeeperClient(
                chats={
                    "control-chat": {"title": "Control"},
                    "indexed-chat": {"title": "Indexed"},
                },
                messages={
                    "control-chat": [
                        {"id": "c1", "sortKey": "1", "timestamp": "2026-06-05T18:00:00Z", "senderName": "Operator", "type": "TEXT", "text": "bootstrap"},
                    ],
                    "indexed-chat": [],
                },
            )
            bridge = ControlBridge(config, api_client=client)
            bridge.process_once()
            client.messages["control-chat"].append(
                {"id": "c2", "sortKey": "2", "timestamp": "2026-06-05T18:01:00Z", "senderName": "Operator", "type": "TEXT", "text": "Remember that Addy is Jordan Lee."}
            )
            bridge.process_once()
            self.assertIn("Please confirm before I save it.", client.sent_messages[-1][1])
            client.messages["control-chat"].append(
                {"id": "c3", "sortKey": "3", "timestamp": "2026-06-05T18:02:00Z", "senderName": "Operator", "type": "TEXT", "text": "yes"}
            )
            result = bridge.process_once()
            self.assertEqual(result.replied_messages, 1)
            self.assertIn("Saved alias: Addy → Jordan Lee.", client.sent_messages[-1][1])

    def test_format_reply_for_chat_converts_markdown(self) -> None:
        from beeper_bot.bridge import format_reply_for_chat

        raw = (
            "## Building Updates\n"
            "**Clubhouse Issues**\n"
            "*   **Wine Dinner:** Wilhelmina reported noise.\n"
            "- Second point here.\n"
            "\n\n\n"
            "Plain paragraph stays."
        )
        formatted = format_reply_for_chat(raw)
        self.assertIn("🔹 Building Updates", formatted)
        self.assertIn("🔹 Clubhouse Issues", formatted)
        self.assertIn("• Wine Dinner: Wilhelmina reported noise.", formatted)
        self.assertIn("• Second point here.", formatted)
        self.assertNotIn("**", formatted)
        self.assertNotIn("##", formatted)
        self.assertNotIn("\n\n\n", formatted)
        # headers get breathing room
        self.assertIn("\n\n🔹 Clubhouse Issues", formatted)

    def test_format_reply_keeps_plain_text_unchanged(self) -> None:
        from beeper_bot.bridge import format_reply_for_chat

        plain = "Taylor sent 123 Sample St [1].\n\nSources:\n[1] Housekeeping — Taylor — 2026-04-19"
        self.assertEqual(format_reply_for_chat(plain), plain)

    def test_split_message_short_text_is_single_part(self) -> None:
        from beeper_bot.bridge import split_message

        self.assertEqual(split_message("just a short answer", 100), ["just a short answer"])

    def test_split_message_breaks_on_paragraphs_with_markers(self) -> None:
        from beeper_bot.bridge import split_message

        text = "\n\n".join(f"Paragraph {i} " + "x" * 40 for i in range(6))
        parts = split_message(text, 120, max_parts=6)
        self.assertGreater(len(parts), 1)
        for idx, part in enumerate(parts, 1):
            self.assertTrue(part.endswith(f"({idx}/{len(parts)})"))
            self.assertLessEqual(len(part), 120)
        # no paragraph was cut mid-block: every part begins at a paragraph
        for part in parts:
            self.assertTrue(part.lstrip().startswith("Paragraph"))

    def test_split_message_hard_splits_giant_token(self) -> None:
        from beeper_bot.bridge import split_message

        text = "x" * 500
        parts = split_message(text, 100, max_parts=10)
        self.assertTrue(all(len(p) <= 100 for p in parts))
        self.assertEqual("".join(p.split("\n(")[0] for p in parts), text)

    def test_split_message_caps_parts_and_marks_truncation(self) -> None:
        from beeper_bot.bridge import split_message

        text = "\n\n".join(f"Block {i} " + "y" * 60 for i in range(20))
        parts = split_message(text, 100, max_parts=3)
        self.assertEqual(len(parts), 3)
        self.assertIn("[truncated]", parts[-1])

    def test_reply_sends_multiple_messages_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir))
            config.bridge.max_reply_chars = 80
            config.bridge.max_reply_parts = 5
            client = FakeBeeperClient(
                chats={"control-chat": {"title": "Control"}, "indexed-chat": {"title": "Indexed"}},
                messages={"control-chat": [], "indexed-chat": []},
            )
            bridge = ControlBridge(config, api_client=client)
            long_text = "\n\n".join(f"Topic {i} " + "z" * 50 for i in range(4))
            returned = bridge._reply(long_text)
            self.assertGreater(len(client.sent_messages), 1)
            # parts are sent to the control chat, in order, each within the cap
            self.assertTrue(all(cid == "control-chat" for cid, _ in client.sent_messages))
            self.assertTrue(all(len(text) <= 80 for _, text in client.sent_messages))
            self.assertTrue(client.sent_messages[0][1].endswith(f"(1/{len(client.sent_messages)})"))
            # returned value (for control-memory) is the clean text, no markers
            self.assertNotIn("(1/", returned)

    def test_confirmation_tolerates_punctuation_and_casing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir))
            client = FakeBeeperClient(
                chats={
                    "control-chat": {"title": "Control"},
                    "indexed-chat": {"title": "Indexed"},
                },
                messages={
                    "control-chat": [
                        {"id": "c1", "sortKey": "1", "timestamp": "2026-06-05T18:00:00Z", "senderName": "Operator", "type": "TEXT", "text": "bootstrap"},
                    ],
                    "indexed-chat": [],
                },
            )
            bridge = ControlBridge(config, api_client=client)
            bridge.process_once()
            client.messages["control-chat"].append(
                {"id": "c2", "sortKey": "2", "timestamp": "2026-06-05T18:01:00Z", "senderName": "Operator", "type": "TEXT", "text": "Remember that Addy is Jordan Lee."}
            )
            bridge.process_once()
            client.messages["control-chat"].append(
                {"id": "c3", "sortKey": "3", "timestamp": "2026-06-05T18:02:00Z", "senderName": "Operator", "type": "TEXT", "text": "Yes, save it."}
            )
            bridge.process_once()
            self.assertIn("Saved alias: Addy → Jordan Lee.", client.sent_messages[-1][1])

    def test_unrelated_message_supersedes_pending_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(self._write_config(tmpdir))
            client = FakeBeeperClient(
                chats={
                    "control-chat": {"title": "Control"},
                    "indexed-chat": {"title": "Indexed"},
                },
                messages={
                    "control-chat": [
                        {"id": "c1", "sortKey": "1", "timestamp": "2026-06-05T18:00:00Z", "senderName": "Operator", "type": "TEXT", "text": "bootstrap"},
                    ],
                    "indexed-chat": [],
                },
            )
            bridge = ControlBridge(config, api_client=client)
            bridge.process_once()
            client.messages["control-chat"].append(
                {"id": "c2", "sortKey": "2", "timestamp": "2026-06-05T18:01:00Z", "senderName": "Operator", "type": "TEXT", "text": "Remember that Addy is Jordan Lee."}
            )
            bridge.process_once()

            import beeper_bot.bridge as bridge_mod
            from beeper_bot.llm import AskResponse
            from beeper_bot.planning import QueryPlan
            from beeper_bot.retrieval import SearchResponse

            original = bridge_mod.ask_archive

            def canned_ask_archive(config, question, **kwargs):
                return AskResponse(
                    question=question,
                    answer="Some unrelated answer.",
                    evidence=[],
                    retrieval=SearchResponse(query=question, results=[]),
                    plan=QueryPlan(normalized_question=question),
                )

            bridge_mod.ask_archive = canned_ask_archive
            try:
                client.messages["control-chat"].append(
                    {"id": "c3", "sortKey": "3", "timestamp": "2026-06-05T18:02:00Z", "senderName": "Operator", "type": "TEXT", "text": "What is the weather like?"}
                )
                bridge.process_once()
                client.messages["control-chat"].append(
                    {"id": "c4", "sortKey": "4", "timestamp": "2026-06-05T18:03:00Z", "senderName": "Operator", "type": "TEXT", "text": "yes"}
                )
                bridge.process_once()
            finally:
                bridge_mod.ask_archive = original
            self.assertNotIn("Saved alias", client.sent_messages[-1][1])


if __name__ == "__main__":
    unittest.main()
