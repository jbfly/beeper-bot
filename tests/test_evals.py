from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import MessagePage
from beeper_bot.config import load_config
from beeper_bot.evals import EvalCase, evaluate_case, format_suite_result, load_eval_suite, run_eval_suite
from beeper_bot.llm import EvidenceItem
from beeper_bot.planning import QueryPlan
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


class FakeLlmClient:
    def __init__(self, answer: str, plan: QueryPlan | None = None):
        self.answer = answer
        self.plan = plan or QueryPlan(
            normalized_question="What address did Seth send?",
            search_queries=["Seth address", "123 Sample St"],
            preferred_senders=["Seth"],
            answer_kind="fact",
            time_hint="any",
        )

    def answer_from_evidence(self, config, question: str, evidence: list[EvidenceItem], person_context: str = "") -> str:
        return self.answer

    def plan_query(self, config, question: str, catalog, graph=None) -> QueryPlan:
        return self.plan


class EvalTest(unittest.TestCase):
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
                    "",
                    "[llm]",
                    "max_input_snippets = 3",
                ]
            )
        )
        return config_path

    def _config_with_data(self):
        tmpdir = tempfile.TemporaryDirectory()
        config = load_config(self._write_config(tmpdir.name, ["chat-a"]))
        client = FakeBeeperClient(
            chats={"chat-a": {"title": "Family logistics"}},
            messages={
                "chat-a": [
                    {
                        "id": "msg-1",
                        "sortKey": "1",
                        "timestamp": "2026-05-11T14:22:00Z",
                        "senderID": "u1",
                        "senderName": "Seth",
                        "type": "TEXT",
                        "text": "The address is 123 Sample St, Portland.",
                    },
                    {
                        "id": "msg-2",
                        "sortKey": "2",
                        "timestamp": "2026-05-12T09:00:00Z",
                        "senderID": "u2",
                        "senderName": "Julie",
                        "type": "TEXT",
                        "text": "Meet there at 5pm.",
                    },
                ]
            },
        )
        sync_chats(config, client)
        return config, tmpdir

    def test_load_eval_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            suite_path = Path(tmpdir) / "suite.json"
            suite_path.write_text(json.dumps({
                "name": "mini",
                "cases": [
                    {"id": "case-1", "question": "What address?", "answer_contains_any": ["123 Sample St"]}
                ],
            }))
            suite = load_eval_suite(suite_path)
            self.assertEqual(suite.name, "mini")
            self.assertEqual(len(suite.cases), 1)
            self.assertEqual(suite.cases[0].case_id, "case-1")

    def test_evaluate_case_passes_with_expected_answer(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        case = EvalCase(
            case_id="address",
            question="What address did Seth send?",
            answer_contains_any=["123 Sample St"],
            evidence_sender_any=["Seth"],
            evidence_chat_any=["Family logistics"],
        )
        result = evaluate_case(config, case, llm_client=FakeLlmClient("Seth sent 123 Sample St [1]."))
        self.assertTrue(result.passed)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.evidence_count, 1)

    def test_run_eval_suite_counts_only_scored_cases(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        suite_path = Path(tmpdir.name) / "suite.json"
        suite_path.write_text(json.dumps({
            "name": "mini",
            "cases": [
                {
                    "id": "pass-case",
                    "question": "What address did Seth send?",
                    "answer_contains_any": ["123 Sample St"],
                    "evidence_sender_any": ["Seth"],
                    "evidence_chat_any": ["Family logistics"]
                },
                {
                    "id": "info-case",
                    "question": "What address did Seth send?",
                    "score_case": False,
                    "answer_contains_any": ["missing"]
                }
            ]
        }))
        suite = load_eval_suite(suite_path)
        result = run_eval_suite(config, suite, llm_client=FakeLlmClient("Seth sent 123 Sample St [1]."))
        self.assertEqual(result.total_cases, 2)
        self.assertEqual(result.scored_cases, 1)
        self.assertEqual(result.passed_cases, 1)
        self.assertEqual(result.failed_cases, 0)
        text = format_suite_result(result)
        self.assertIn("PASS pass-case", text)
        self.assertIn("INFO info-case", text)


if __name__ == "__main__":
    unittest.main()
