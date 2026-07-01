from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import MessagePage
from beeper_bot.config import load_config
from beeper_bot.evals import (
    EvalCase,
    configure_eval_run,
    evaluate_case,
    format_suite_result,
    load_eval_suite,
    run_eval_suite,
    suite_result_to_dict,
)
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

    def answer_from_evidence(
        self,
        config,
        question: str,
        evidence: list[EvidenceItem],
        person_context: str = "",
        control_context: str = "",
    ) -> str:
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
                    {
                        "id": "case-1",
                        "question": "What address?",
                        "answer_contains_any": ["123 Sample St"],
                        "plan_answer_kind_any": ["fact"],
                        "plan_preferred_sender_any": ["Seth"],
                        "metrics_only": True,
                        "control_turns": [
                            {"role": "user", "content": "Remember that Alex is my sister."},
                            {"role": "assistant", "content": "Okay, I can store that after confirmation."}
                        ],
                        "memory_state": {
                            "facts": [{"subject": "Anna", "predicate": "relationship", "object": "sister"}]
                        },
                        "expected_actions": ["confirm-memory-write"],
                        "expected_sources": ["memory"],
                        "context_budget_class": "short"
                    }
                ],
            }))
            suite = load_eval_suite(suite_path)
            self.assertEqual(suite.name, "mini")
            self.assertEqual(len(suite.cases), 1)
            self.assertEqual(suite.cases[0].case_id, "case-1")
            self.assertEqual(suite.cases[0].plan_answer_kind_any, ["fact"])
            self.assertEqual(suite.cases[0].plan_preferred_sender_any, ["Seth"])
            self.assertTrue(suite.cases[0].metrics_only)
            self.assertEqual(suite.cases[0].control_turns[0]["role"], "user")
            self.assertEqual(suite.cases[0].memory_state["facts"][0]["object"], "sister")
            self.assertEqual(suite.cases[0].expected_actions, ["confirm-memory-write"])
            self.assertEqual(suite.cases[0].expected_sources, ["memory"])
            self.assertEqual(suite.cases[0].context_budget_class, "short")

    def test_evaluate_case_passes_with_expected_answer(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        case = EvalCase(
            case_id="address",
            question="What address did Seth send?",
            answer_contains_any=["123 Sample St"],
            answer_not_contains=["insufficient"],
            evidence_sender_any=["Seth"],
            evidence_chat_any=["Family logistics"],
            plan_preferred_sender_any=["Seth"],
            plan_answer_kind_any=["fact"],
            plan_time_hint_any=["any"],
        )
        result = evaluate_case(config, case, llm_client=FakeLlmClient("Seth sent 123 Sample St [1]."))
        self.assertTrue(result.passed)
        self.assertEqual(result.failures, [])
        self.assertGreaterEqual(result.evidence_count, 1)
        self.assertIn("archive", result.inferred_sources)

    def test_evaluate_case_accepts_memory_answer_without_archive_citation(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        case = EvalCase(
            case_id="memory",
            question="Who is Alex again?",
            min_evidence=0,
            require_citation=False,
            answer_contains_any=["sister"],
            expected_sources=["memory"],
            memory_state={
                "facts": [
                    {"subject": "Alex Morgan", "predicate": "relationship_to_user", "object": "sister", "source": "user-approved fact"}
                ]
            },
        )
        result = evaluate_case(config, case, llm_client=FakeLlmClient("Alex Morgan is your sister."))
        self.assertTrue(result.passed)
        self.assertIn("memory", result.inferred_sources)
        self.assertNotIn("archive", result.inferred_sources)

    def test_evaluate_case_rejects_fake_archive_citation_for_memory_only_case(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        case = EvalCase(
            case_id="memory-fake-citation",
            question="Tell me about Anna from memory.",
            min_evidence=0,
            require_citation=False,
            answer_contains_any=["sister"],
            expected_sources=["memory"],
            memory_state={
                "facts": [
                    {"subject": "Alex Morgan", "predicate": "relationship_to_user", "object": "sister", "source": "user-approved fact"}
                ]
            },
        )
        result = evaluate_case(config, case, llm_client=FakeLlmClient("Alex Morgan is your sister [1]."))
        self.assertFalse(result.passed)
        self.assertIn("answer used archive-style citation where archive source was not expected", result.failures)

    def test_configure_eval_run_can_force_deterministic_sampling(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        self.assertNotEqual(config.llm.temperature, 0.0)
        configured = configure_eval_run(config, deterministic=True)
        self.assertEqual(configured.llm.temperature, 0.0)
        self.assertEqual(configured.llm.planner_temperature, 0.0)
        self.assertEqual(config.llm.temperature, 0.1)

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
                    "answer_not_contains": ["insufficient"],
                    "evidence_sender_any": ["Seth"],
                    "evidence_chat_any": ["Family logistics"],
                    "plan_preferred_sender_any": ["Seth"],
                    "plan_answer_kind_any": ["fact"]
                },
                {
                    "id": "info-case",
                    "question": "What address did Seth send?",
                    "score_case": False,
                    "answer_contains_any": ["missing"]
                },
                {
                    "id": "metric-case",
                    "question": "What address did Seth send?",
                    "metrics_only": True,
                    "answer_contains_any": ["missing"]
                }
            ]
        }))
        suite = load_eval_suite(suite_path)
        result = run_eval_suite(config, suite, llm_client=FakeLlmClient("Seth sent 123 Sample St [1]."))
        self.assertEqual(result.total_cases, 3)
        self.assertEqual(result.scored_cases, 1)
        self.assertEqual(result.passed_cases, 1)
        self.assertEqual(result.failed_cases, 0)
        self.assertIn("temperature", result.runtime)
        text = format_suite_result(result)
        self.assertIn("Runtime:", text)
        self.assertIn("PASS pass-case", text)
        self.assertIn("INFO info-case", text)
        self.assertIn("METRIC metric-case", text)

    def test_run_eval_suite_emits_family_summary_for_context_ladder(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        suite_path = Path(tmpdir.name) / "suite.json"
        suite_path.write_text(json.dumps({
            "name": "context-ladder",
            "cases": [
                {
                    "id": "family_short",
                    "question": "What address did Seth send?",
                    "context_budget_class": "short",
                    "answer_contains_any": ["123 Sample St"]
                },
                {
                    "id": "family_medium",
                    "question": "What address did Seth send?",
                    "context_budget_class": "medium",
                    "answer_contains_any": ["missing"]
                },
                {
                    "id": "family_stress",
                    "question": "What address did Seth send?",
                    "context_budget_class": "stress",
                    "metrics_only": True,
                    "score_case": False,
                    "answer_contains_any": ["missing"]
                }
            ]
        }))
        suite = load_eval_suite(suite_path)
        result = run_eval_suite(config, suite, llm_client=FakeLlmClient("Seth sent 123 Sample St [1]."))
        self.assertEqual(len(result.family_summaries), 1)
        family = result.family_summaries[0]
        self.assertEqual(family.family_id, "family")
        self.assertEqual(family.scored_cases, 2)
        self.assertEqual(family.passed_cases, 1)
        self.assertEqual(family.failed_cases, 1)
        self.assertEqual(family.first_failed_rung, "medium")
        self.assertEqual(family.rung_results["short"]["passed"], True)
        self.assertEqual(family.rung_results["stress"]["metrics_only"], True)
        text = format_suite_result(result)
        self.assertIn("Family summary:", text)
        self.assertIn("family: passed=1/2 first_failed_rung=medium", text)
        payload = suite_result_to_dict(result)
        self.assertEqual(payload["family_summaries"][0]["family_id"], "family")

    def test_format_suite_result_shows_expected_and_inferred_sources(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        suite_path = Path(tmpdir.name) / "suite.json"
        suite_path.write_text(json.dumps({
            "name": "control-memory",
            "cases": [
                {
                    "id": "memory_case",
                    "question": "Who is Alex again?",
                    "expected_sources": ["memory"],
                    "memory_state": {
                        "facts": [
                            {"subject": "Alex Morgan", "predicate": "relationship_to_user", "object": "sister", "source": "user-approved fact"}
                        ]
                    },
                    "answer_contains_any": ["sister"]
                }
            ]
        }))
        suite = load_eval_suite(suite_path)
        result = run_eval_suite(config, suite, llm_client=FakeLlmClient("Alex Morgan is your sister."))
        text = format_suite_result(result)
        self.assertIn("Sources: expected=['memory'] inferred=['memory']", text)

    def test_evaluate_case_checks_expected_actions_for_alias_confirmation(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        case = EvalCase(
            case_id="alias-add",
            question="Remember that Addy is Jordan Lee.",
            min_evidence=0,
            require_citation=False,
            expected_actions=["confirm-memory-write", "add-alias"],
            answer_contains_any=["confirm", "alias"],
        )
        result = evaluate_case(
            config,
            case,
            llm_client=FakeLlmClient("I can save that as an alias for Jordan Lee. Please confirm before I save it."),
        )
        self.assertTrue(result.passed)
        self.assertIn("confirm-memory-write", result.inferred_actions)
        self.assertIn("add-alias", result.inferred_actions)

    def test_evaluate_case_fails_when_expected_action_is_missing(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        case = EvalCase(
            case_id="alias-add-missing",
            question="Please store that Addy is Jordan Lee.",
            min_evidence=0,
            require_citation=False,
            expected_actions=["confirm-memory-write", "add-alias"],
            answer_contains_any=["saved"],
        )
        result = evaluate_case(
            config,
            case,
            llm_client=FakeLlmClient("Okay, saved."),
        )
        self.assertFalse(result.passed)
        self.assertIn("answer missing expected actions: add-alias, confirm-memory-write", result.failures)

    def test_model_scored_case_fails_when_resolved_by_direct_path(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        case = EvalCase(
            case_id="memory-lookup-direct-path",
            question="Who is Alex again?",
            min_evidence=0,
            require_citation=False,
            expected_path="model",
            memory_state={
                "facts": [
                    {
                        "subject": "Alex Morgan",
                        "predicate": "relationship_to_user",
                        "object": "sister",
                        "source": "user-approved fact",
                    }
                ]
            },
            answer_contains_any=["sister"],
        )
        result = evaluate_case(config, case, llm_client=FakeLlmClient("unused"))
        self.assertEqual(result.answer_path, "direct")
        self.assertFalse(result.passed)
        self.assertIn("answer path was 'direct', expected 'model'", result.failures)

    def test_direct_path_case_passes_when_expected(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        case = EvalCase(
            case_id="memory-lookup-direct-ok",
            question="Who is Alex again?",
            min_evidence=0,
            require_citation=False,
            expected_path="direct",
            memory_state={
                "facts": [
                    {
                        "subject": "Alex Morgan",
                        "predicate": "relationship_to_user",
                        "object": "sister",
                        "source": "user-approved fact",
                    }
                ]
            },
            answer_contains_any=["sister"],
        )
        result = evaluate_case(config, case, llm_client=FakeLlmClient("unused"))
        self.assertEqual(result.answer_path, "direct")
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
