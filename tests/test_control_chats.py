from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from beeper_bot.beeper_api import MessagePage
from beeper_bot.bridge import ControlBridge
from beeper_bot.config import load_config, resolved_control_chats
from beeper_bot.db import enqueue_outbound, open_db


class FakeBeeperClient:
    def __init__(self, messages: dict[str, list[dict]]):
        self.messages = messages
        self.sent_messages: list[tuple[str, str]] = []

    def fetch_chat(self, chat_id: str) -> dict:
        return {"title": chat_id}

    def fetch_messages(self, chat_id: str) -> list[dict]:
        return list(self.messages.get(chat_id, []))

    def fetch_messages_page(self, chat_id: str, cursor: str | None = None, direction: str | None = None) -> MessagePage:
        return MessagePage(items=list(self.messages.get(chat_id, [])), has_more=False, oldest_cursor=None, newest_cursor=None)

    def send_message(self, chat_id: str, text: str) -> None:
        self.sent_messages.append((chat_id, text))


def _msg(idx: int, text: str) -> dict:
    return {
        "id": f"m{idx}",
        "sortKey": str(idx),
        "timestamp": f"2026-06-05T18:0{idx}:00Z",
        "senderName": "Operator",
        "type": "TEXT",
        "text": text,
    }


def _write_config(tmpdir: str, extra: list[str] | None = None) -> Path:
    config_path = Path(tmpdir) / "config.toml"
    db_path = Path(tmpdir) / "archive.sqlite3"
    token_path = Path(tmpdir) / "token"
    token_path.write_text("dummy-token\n")
    lines = [
        "[beeper]",
        'api_base = "http://localhost:23373/v1"',
        f'token_file = "{token_path}"',
        'control_chat_id = "main-chat"',
        "poll_seconds = 1",
        "",
        "[archive]",
        f'path = "{db_path}"',
        "",
        "[llm]",
        'base_url = "http://127.0.0.1:8090/v1"',
        'model = "gemma"',
        "",
        "[bridge]",
        'reply_prefix = "[BEEPER-BOT] "',
        "max_reply_chars = 3500",
    ]
    lines.extend(extra or [])
    config_path.write_text("\n".join(lines))
    return config_path


class ResolverTest(unittest.TestCase):
    def test_legacy_control_chat_id_becomes_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp))
            chats = resolved_control_chats(config)
            self.assertEqual([c.name for c in chats], ["main"])
            self.assertEqual(chats[0].chat_id, "main-chat")

    def test_named_control_chats_join_the_legacy_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp, [
                "",
                "[control_chats.translate]",
                'chat_id = "translate-chat"',
                'persona = "You translate."',
                'allowed_commands = ["ask", "help"]',
            ]))
            chats = resolved_control_chats(config)
            names = {c.name: c for c in chats}
            self.assertEqual(set(names), {"main", "translate"})
            self.assertEqual(names["translate"].persona, "You translate.")
            self.assertEqual(names["translate"].allowed_commands, ["ask", "help"])

    def test_explicit_main_wins_over_legacy_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp, [
                "",
                "[control_chats.main]",
                'chat_id = "explicit-main"',
            ]))
            chats = resolved_control_chats(config)
            self.assertEqual([c.chat_id for c in chats], ["explicit-main"])

    def test_duplicate_chat_id_is_polled_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp, [
                "",
                "[control_chats.alias]",
                'chat_id = "main-chat"',  # same id as legacy control_chat_id
            ]))
            chats = resolved_control_chats(config)
            self.assertEqual([c.chat_id for c in chats], ["main-chat"])


class MultiChatServeTest(unittest.TestCase):
    def test_each_chat_has_its_own_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp, [
                "",
                "[control_chats.second]",
                'chat_id = "second-chat"',
            ]))
            client = FakeBeeperClient({
                "main-chat": [_msg(1, "hello main")],
                "second-chat": [_msg(1, "hello second")],
            })
            bridge = ControlBridge(config, api_client=client)
            # First pass seeds both cursors to 'now' -> no replies.
            bridge.process_once()
            self.assertEqual(client.sent_messages, [])
            # A new message in only the second chat is picked up there.
            client.messages["second-chat"].append(_msg(2, "/help"))
            bridge.process_once()
            self.assertEqual(len(client.sent_messages), 1)
            self.assertEqual(client.sent_messages[-1][0], "second-chat")
            self.assertIn("Commands:", client.sent_messages[-1][1])

    def test_persona_flows_into_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp, [
                "",
                "[control_chats.translate]",
                'chat_id = "translate-chat"',
                'persona = "You are a translator."',
            ]))
            client = FakeBeeperClient({"main-chat": [], "translate-chat": [_msg(1, "seed")]})
            bridge = ControlBridge(config, api_client=client)
            bridge.process_once()

            import beeper_bot.bridge as bridge_mod
            from beeper_bot.llm import AskResponse
            from beeper_bot.planning import QueryPlan
            from beeper_bot.retrieval import SearchResponse

            captured: dict[str, str] = {}
            original = bridge_mod.ask_archive

            def spy_ask_archive(config, question, **kwargs):
                captured["persona"] = kwargs.get("persona", "")
                return AskResponse(
                    question=question, answer="ok", evidence=[],
                    retrieval=SearchResponse(query=question, results=[]),
                    plan=QueryPlan(normalized_question=question),
                )

            bridge_mod.ask_archive = spy_ask_archive
            try:
                client.messages["translate-chat"].append(_msg(2, "traduz isto"))
                bridge.process_once()
            finally:
                bridge_mod.ask_archive = original
            self.assertEqual(captured.get("persona"), "You are a translator.")

    def test_allowed_commands_filters_disallowed_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp, [
                "",
                "[control_chats.feed]",
                'chat_id = "feed-chat"',
                'allowed_commands = ["status"]',  # ask/plain-text not allowed
            ]))
            client = FakeBeeperClient({"main-chat": [], "feed-chat": [_msg(1, "seed")]})
            bridge = ControlBridge(config, api_client=client)
            bridge.process_once()
            client.messages["feed-chat"].append(_msg(2, "hello there"))  # -> ask, disallowed
            result = bridge.process_once()
            # Message was seen but produced no reply (silently ignored).
            self.assertEqual([m for m in client.sent_messages if m[0] == "feed-chat"], [])
            self.assertEqual(result.replied_messages, 0)


class OutboundQueueTest(unittest.TestCase):
    def test_notify_is_delivered_once_by_serve_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp))
            client = FakeBeeperClient({"main-chat": []})
            bridge = ControlBridge(config, api_client=client)
            from beeper_bot.db import init_db_path
            init_db_path(config.archive.path)
            with open_db(config.archive.path) as conn:
                enqueue_outbound(conn, "main", "backup finished")

            bridge.process_once()
            self.assertIn(("main-chat", "backup finished"), client.sent_messages)
            # Not re-sent on the next pass.
            client.sent_messages.clear()
            bridge.process_once()
            self.assertEqual(client.sent_messages, [])

    def test_permission_denial_drops_outbound_and_logs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp))
            client = FakeBeeperClient({"main-chat": []})
            client.send_message = Mock(side_effect=PermissionError("sending disabled"))
            bridge = ControlBridge(config, api_client=client)
            from beeper_bot.db import init_db_path
            init_db_path(config.archive.path)
            with open_db(config.archive.path) as conn:
                enqueue_outbound(conn, "main", "blocked notification")

            with patch("beeper_bot.bridge.log") as bridge_log:
                bridge._drain_outbound()
                bridge._drain_outbound()

            with open_db(config.archive.path) as conn:
                self.assertEqual(len(conn.execute(
                    "SELECT id FROM outbound_queue WHERE sent_at IS NULL"
                ).fetchall()), 0)
            bridge_log.assert_called_once_with(
                "outbound permanently failed id=1 target=main: "
                "sending is disabled by [security] allow_send",
                level="error",
            )

    def test_notify_to_named_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(_write_config(tmp, [
                "",
                "[control_chats.cams]",
                'chat_id = "cams-chat"',
            ]))
            client = FakeBeeperClient({"main-chat": [], "cams-chat": []})
            bridge = ControlBridge(config, api_client=client)
            from beeper_bot.db import init_db_path
            init_db_path(config.archive.path)
            with open_db(config.archive.path) as conn:
                enqueue_outbound(conn, "cams", "motion detected")
            bridge.process_once()
            self.assertIn(("cams-chat", "motion detected"), client.sent_messages)


class NotifyCliTest(unittest.TestCase):
    def test_notify_command_enqueues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_config(tmp)
            from beeper_bot.cli import main
            rc = main(["--config", str(config_path), "notify", "hello", "world", "--chat", "main"])
            self.assertEqual(rc, 0)
            config = load_config(config_path)
            with open_db(config.archive.path) as conn:
                rows = conn.execute("SELECT target, text, sent_at FROM outbound_queue").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["target"], "main")
            self.assertEqual(rows[0]["text"], "hello world")
            self.assertIsNone(rows[0]["sent_at"])


if __name__ == "__main__":
    unittest.main()
