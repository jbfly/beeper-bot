from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import beeper_bot.music as music
from beeper_bot.config import AppConfig, MusicConfig


def _config(project_root: Path) -> AppConfig:
    cfg = AppConfig(config_path=Path("/tmp/x"))
    cfg.music = MusicConfig(project_root=project_root, host_python="/usr/bin/python3", max_tool_iterations=3)
    return cfg


def _write_queue(root: Path, items: list[dict], garbage: str = "") -> None:
    qdir = root / "state" / "fixer_queue"
    qdir.mkdir(parents=True)
    lines = [json.dumps(i) for i in items]
    if garbage:
        lines.insert(1, garbage)
    (qdir / "issues.jsonl").write_text("\n".join(lines) + "\n")


class ReadIssuesTest(unittest.TestCase):
    def test_skips_garbage_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_queue(root, [
                {"id": "a", "status": "new", "text": "x"},
                {"id": "b", "status": "done", "text": "y"},
            ], garbage='{"torn": ')
            items = music.read_issues(_config(root))
        self.assertEqual([i["id"] for i in items], ["a", "b"])

    def test_missing_file_is_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(music.read_issues(_config(Path(tmp))), [])


class RunMusicToolTest(unittest.TestCase):
    def test_queue_list_filters_status(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_queue(root, [
                {"id": "a", "status": "new", "text": "x"},
                {"id": "b", "status": "done", "text": "y"},
            ])
            out, is_err = music.run_music_tool(_config(root), "queue_list", {"status": "done"})
        self.assertFalse(is_err)
        self.assertEqual([i["id"] for i in json.loads(out)], ["b"])

    def test_queue_list_rejects_bad_status(self) -> None:
        with TemporaryDirectory() as tmp:
            out, is_err = music.run_music_tool(_config(Path(tmp)), "queue_list", {"status": "weird"})
        self.assertTrue(is_err)

    def test_resolve_issue_validates_id(self) -> None:
        with TemporaryDirectory() as tmp:
            out, is_err = music.run_music_tool(_config(Path(tmp)), "resolve_issue", {"id": "../../etc", "note": "n"})
        self.assertTrue(is_err)
        self.assertIn("invalid issue id", out)

    def test_unknown_tool_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            out, is_err = music.run_music_tool(_config(Path(tmp)), "rm_rf", {})
        self.assertTrue(is_err)

    def test_argv_construction(self) -> None:
        calls: list[list[str]] = []

        class FakeProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        orig = music.subprocess.run
        music.subprocess.run = lambda argv, **kw: calls.append(argv) or FakeProc()
        try:
            with TemporaryDirectory() as tmp:
                cfg = _config(Path(tmp))
                music.run_music_tool(cfg, "diagnose_track", {"path_or_beets_id": "/mnt/music-synology/x.mp3"})
                music.run_music_tool(cfg, "capture_issue", {"text": "needs upgrade"})
                music.run_music_tool(cfg, "resolve_issue", {"id": "20260707-121458-4fbc", "note": "done"})
        finally:
            music.subprocess.run = orig
        self.assertEqual(calls[0][:4], ["docker", "exec", "music-beets", "python3"])
        self.assertIn("needs upgrade", calls[1])
        self.assertEqual(calls[2][-3:], ["--resolve", "20260707-121458-4fbc", "done"])


class MusicChatTurnTest(unittest.TestCase):
    def _patched(self, responses: list[dict]):
        sent: list[list[dict]] = []

        def fake_anthropic(config, messages, **kw):
            sent.append([dict(m) for m in messages])
            return responses[min(len(sent) - 1, len(responses) - 1)]

        return fake_anthropic, sent

    def test_tool_loop_executes_and_replies(self) -> None:
        responses = [
            {"stop_reason": "tool_use", "content": [
                {"type": "thinking", "thinking": "", "signature": "s"},
                {"type": "tool_use", "id": "tu1", "name": "queue_list", "input": {"status": "new"}},
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "2 open issues."}]},
        ]
        fake, sent = self._patched(responses)
        orig_llm, orig_ctx = music.anthropic_messages, music._context_block
        music.anthropic_messages = fake
        music._context_block = lambda cfg: "ctx"
        try:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_queue(root, [{"id": "a", "status": "new", "text": "x"}])
                reply = music.music_chat_turn(_config(root), "what's in the queue?")
        finally:
            music.anthropic_messages, music._context_block = orig_llm, orig_ctx
        self.assertEqual(reply, "2 open issues.")
        # second call carries assistant echo (thinking incl.) + tool_result
        second = sent[1]
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertEqual(second[-2]["content"][0]["type"], "thinking")
        result = second[-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["tool_use_id"], "tu1")
        self.assertNotIn("is_error", result)

    def test_unknown_tool_returns_is_error(self) -> None:
        responses = [
            {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "tu1", "name": "nope", "input": {}},
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "sorry"}]},
        ]
        fake, sent = self._patched(responses)
        orig_llm, orig_ctx = music.anthropic_messages, music._context_block
        music.anthropic_messages = fake
        music._context_block = lambda cfg: "ctx"
        try:
            with TemporaryDirectory() as tmp:
                music.music_chat_turn(_config(Path(tmp)), "hi")
        finally:
            music.anthropic_messages, music._context_block = orig_llm, orig_ctx
        self.assertTrue(sent[1][-1]["content"][0]["is_error"])

    def test_loop_cap(self) -> None:
        responses = [
            {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "tu", "name": "queue_list", "input": {}},
            ]},
        ]
        fake, sent = self._patched(responses)
        orig_llm, orig_ctx = music.anthropic_messages, music._context_block
        music.anthropic_messages = fake
        music._context_block = lambda cfg: "ctx"
        try:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_queue(root, [])
                reply = music.music_chat_turn(_config(root), "loop forever")
        finally:
            music.anthropic_messages, music._context_block = orig_llm, orig_ctx
        self.assertEqual(len(sent), 3)  # max_tool_iterations in _config
        self.assertIn("tool budget", reply)

    def test_history_drops_leading_assistant(self) -> None:
        responses = [{"stop_reason": "end_turn", "content": [{"type": "text", "text": "hey"}]}]
        fake, sent = self._patched(responses)
        orig_llm, orig_ctx = music.anthropic_messages, music._context_block
        music.anthropic_messages = fake
        music._context_block = lambda cfg: "ctx"
        try:
            with TemporaryDirectory() as tmp:
                music.music_chat_turn(
                    _config(Path(tmp)), "hi",
                    turns=[{"role": "assistant", "content": "stale"}, {"role": "user", "content": "earlier"}],
                )
        finally:
            music.anthropic_messages, music._context_block = orig_llm, orig_ctx
        self.assertEqual(sent[0][0], {"role": "user", "content": "earlier"})
        self.assertEqual(sent[0][-1], {"role": "user", "content": "hi"})


class _FakeBeeperClient:
    def __init__(self, messages: dict[str, list[dict]]):
        self.messages = messages
        self.sent_messages: list[tuple[str, str]] = []

    def fetch_chat(self, chat_id: str) -> dict:
        return {"title": chat_id}

    def fetch_messages(self, chat_id: str) -> list[dict]:
        return list(self.messages.get(chat_id, []))

    def send_message(self, chat_id: str, text: str) -> None:
        self.sent_messages.append((chat_id, text))


def _music_bridge(tmp: str):
    from beeper_bot.bridge import ControlBridge
    from beeper_bot.config import load_config

    config_path = Path(tmp) / "config.toml"
    (Path(tmp) / "token").write_text("t\n")
    config_path.write_text("\n".join([
        "[beeper]",
        f'token_file = "{Path(tmp) / "token"}"',
        'control_chat_id = "main-chat"',
        "[archive]",
        f'path = "{Path(tmp) / "archive.sqlite3"}"',
        "[control_chats.music]",
        'chat_id = "music-chat"',
        'allowed_commands = ["music", "music-status", "ask", "help", "status"]',
        "[music]",
        f'project_root = "{tmp}"',
    ]))
    config = load_config(config_path)
    client = _FakeBeeperClient({"main-chat": [], "music-chat": []})
    return ControlBridge(config, api_client=client), client


def _msg(idx: int, text: str) -> dict:
    return {"id": f"m{idx}", "sortKey": str(idx), "timestamp": f"2026-07-07T18:0{idx}:00Z",
            "senderName": "Operator", "type": "TEXT", "text": text}


class MusicChatRoutingTest(unittest.TestCase):
    def test_free_text_in_music_chat_runs_tool_loop(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge, client = _music_bridge(tmp)
            client.messages["music-chat"].append(_msg(1, "seed"))
            bridge.process_once()  # seeds cursors

            captured: dict[str, str] = {}

            def fake_turn(config, text, turns=None):
                captured["text"] = text
                captured["history_len"] = len(turns or [])
                return "the queue has 2 items"

            orig = music.music_chat_turn
            music.music_chat_turn = fake_turn
            try:
                client.messages["music-chat"].append(_msg(2, "what's in the queue?"))
                bridge.process_once()
            finally:
                music.music_chat_turn = orig
            self.assertEqual(captured["text"], "what's in the queue?")
            self.assertEqual(client.sent_messages[-1][0], "music-chat")
            self.assertIn("the queue has 2 items", client.sent_messages[-1][1])

    def test_llm_error_falls_back_to_command_hint(self) -> None:
        from beeper_bot.llm import LlmError

        with TemporaryDirectory() as tmp:
            bridge, client = _music_bridge(tmp)
            client.messages["music-chat"].append(_msg(1, "seed"))
            bridge.process_once()

            def broken_turn(config, text, turns=None):
                raise LlmError("no key")

            orig = music.music_chat_turn
            music.music_chat_turn = broken_turn
            try:
                client.messages["music-chat"].append(_msg(2, "hello?"))
                bridge.process_once()
            finally:
                music.music_chat_turn = orig
            self.assertIn("music brain is offline", client.sent_messages[-1][1])

    def test_music_status_command_shells_to_fixer_capture(self) -> None:
        import subprocess as sp
        from unittest import mock

        with TemporaryDirectory() as tmp:
            bridge, client = _music_bridge(tmp)
            client.messages["music-chat"].append(_msg(1, "seed"))
            bridge.process_once()

            fake = mock.Mock(return_value=mock.Mock(returncode=0, stdout="Fixer queue: 1 open.", stderr=""))
            with mock.patch.object(sp, "run", fake):
                client.messages["music-chat"].append(_msg(2, "/music-status"))
                bridge.process_once()
            argv = fake.call_args[0][0]
            self.assertIn("--status", argv)
            self.assertIn("Fixer queue: 1 open.", client.sent_messages[-1][1])

    def test_main_chat_free_text_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            bridge, client = _music_bridge(tmp)
            client.messages["main-chat"].append(_msg(1, "seed"))
            bridge.process_once()

            called = {"music": False}

            def fake_turn(config, text, turns=None):
                called["music"] = True
                return "x"

            import beeper_bot.bridge as bridge_mod
            orig_turn = music.music_chat_turn
            orig_ask = bridge_mod.ask_archive

            from beeper_bot.llm import AskResponse
            from beeper_bot.planning import QueryPlan
            from beeper_bot.retrieval import SearchResponse

            def fake_ask(config, question, **kw):
                return AskResponse(question=question, answer="archive answer", evidence=[],
                                   retrieval=SearchResponse(query=question, results=[]),
                                   plan=QueryPlan(normalized_question=question))

            music.music_chat_turn = fake_turn
            bridge_mod.ask_archive = fake_ask
            try:
                client.messages["main-chat"].append(_msg(2, "what did Seth say?"))
                bridge.process_once()
            finally:
                music.music_chat_turn = orig_turn
                bridge_mod.ask_archive = orig_ask
            self.assertFalse(called["music"])
            self.assertIn("archive answer", client.sent_messages[-1][1])


if __name__ == "__main__":
    unittest.main()
