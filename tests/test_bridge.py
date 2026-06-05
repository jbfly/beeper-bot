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
                    'base_url = "http://127.0.0.1:8085/v1"',
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
                    "indexed-chat": {"title": "Thomas Wright"},
                },
                messages={
                    "control-chat": [
                        {"id": "c1", "sortKey": "1", "timestamp": "2026-06-05T18:00:00Z", "senderName": "JBFLY", "type": "TEXT", "text": "/help"},
                    ],
                    "indexed-chat": [
                        {"id": "m1", "sortKey": "10", "timestamp": "2026-06-03T18:10:58.000Z", "senderID": "u1", "senderName": "Thomas Wright", "type": "TEXT", "text": "Ryzen 7 5800X3D AM4 10th Anniversary Edition"},
                    ],
                },
            )
            bridge = ControlBridge(config, api_client=client)

            first = bridge.process_once()
            self.assertEqual(first.processed_messages, 0)
            self.assertEqual(client.sent_messages, [])

            client.messages["control-chat"].append(
                {"id": "c2", "sortKey": "2", "timestamp": "2026-06-05T18:01:00Z", "senderName": "JBFLY", "type": "TEXT", "text": "/help"}
            )
            second = bridge.process_once()
            self.assertEqual(second.replied_messages, 1)
            self.assertIn("Commands:", client.sent_messages[-1][1])

            client.messages["control-chat"].append(
                {"id": "c3", "sortKey": "3", "timestamp": "2026-06-05T18:02:00Z", "senderName": "JBFLY", "type": "TEXT", "text": "/find 5800X3D"}
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
                    "indexed-chat": {"title": "Thomas Wright"},
                },
                messages={
                    "control-chat": [
                        {"id": "c1", "sortKey": "1", "timestamp": "2026-06-05T18:00:00Z", "senderName": "JBFLY", "type": "TEXT", "text": "bootstrap"},
                    ],
                    "indexed-chat": [],
                },
            )
            bridge = ControlBridge(config, api_client=client)
            bridge.process_once()
            client.messages["control-chat"].append(
                {"id": "c2", "sortKey": "2", "timestamp": "2026-06-05T18:01:00Z", "senderName": "JBFLY", "type": "TEXT", "text": "What did Thomas send?"}
            )

            import beeper_bot.bridge as bridge_mod
            original = bridge_mod.ask_archive
            def raising_ask_archive(config, question):
                raise LlmError("LLM API failed: <urlopen error [Errno 111] Connection refused>")
            bridge_mod.ask_archive = raising_ask_archive
            try:
                result = bridge.process_once()
            finally:
                bridge_mod.ask_archive = original
            self.assertEqual(result.replied_messages, 1)
            self.assertIn("local model is starting up", client.sent_messages[-1][1].lower())


if __name__ == "__main__":
    unittest.main()
