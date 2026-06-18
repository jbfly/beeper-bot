from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import MessagePage
from beeper_bot.config import load_config
from beeper_bot.llm import (
    AskResponse,
    EvidenceItem,
    LlmError,
    _repair_address_answer_from_retrieval,
    _repair_list_answer_from_evidence,
    ask_archive,
    build_answer_prompt,
    build_evidence_packet,
    build_slice_evidence_packet,
    build_slice_reasoning_prompt,
    format_ask_response,
)
from beeper_bot.planning import QueryPlan
from beeper_bot.retrieval import pack_chat_windows, search_archive_multi
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
            normalized_question="",
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


class LlmTest(unittest.TestCase):
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
                    {
                        "id": "msg-3",
                        "sortKey": "3",
                        "timestamp": "2026-05-12T10:00:00Z",
                        "senderID": "u1",
                        "senderName": "Seth",
                        "type": "TEXT",
                        "text": "Please check whether the store has bottom sheets.",
                    },
                ]
            },
        )
        sync_chats(config, client)
        return config, tmpdir

    def test_build_evidence_packet(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "What address did Seth send?",
            llm_client=FakeLlmClient("It was 123 Sample St [1]."),
        )
        self.assertGreaterEqual(len(response.evidence), 1)
        self.assertEqual(response.evidence[0].citation_id, "[1]")
        self.assertEqual(response.evidence[0].message_id, "msg-1")

    def test_build_answer_prompt(self) -> None:
        evidence = [
            EvidenceItem("[1]", "msg-1", "chat-a", "Family logistics", "Seth", "2026-05-11T14:22:00Z", "The address is 123 Sample St.", 10.0)
        ]
        prompt = build_answer_prompt(
            "What address?",
            evidence,
            control_context="Recent control-chat turns:\n- user: What address did Seth send?",
        )
        self.assertIn("Question:\nWhat address?", prompt)
        self.assertIn("[1] [Family logistics]", prompt)
        self.assertIn("Control-memory context:", prompt)

    def test_build_evidence_packet_prefers_relevant_line_in_long_message(self) -> None:
        long_text = (
            "Welcome to the house.<br><br>"
            "Keys will be in the key box by the front door.<br><br>"
            "Check in starts from 2pm onwards.<br><br>"
            "Please send proof of payment to host@example.test before arrival."
        )
        evidence = build_evidence_packet(
            [
                type("R", (), {
                    "message_id": "msg-long",
                    "chat_id": "chat-a",
                    "chat_name": "Family logistics",
                    "sender_name": "Seth",
                    "timestamp": "2026-05-11T14:22:00Z",
                    "text": long_text,
                    "score": 10.0,
                    "context_before": [],
                    "context_after": [],
                })()
            ],
            1,
            question="What email did the note say to send proof of payment to?",
        )
        self.assertIn("host@example.test", evidence[0].excerpt)
        self.assertNotIn("Welcome to the house.", evidence[0].excerpt)

    def test_format_ask_response_appends_sources(self) -> None:
        response = AskResponse(
            question="What address?",
            answer="It was 123 Sample St [1] [9].",
            evidence=[
                EvidenceItem("[1]", "msg-1", "chat-a", "Family logistics", "Seth", "2026-05-11T14:22:00Z", "The address is 123 Sample St.", 10.0)
            ],
            retrieval=None,  # type: ignore[arg-type]
            plan=QueryPlan(normalized_question="What address?", search_queries=["address"], answer_kind="fact", time_hint="any"),
        )
        text = format_ask_response(response)
        self.assertIn("It was 123 Sample St [1].", text)
        self.assertNotIn("[9]", text)
        self.assertIn("Sources:", text)
        self.assertEqual(text.count("\n[1] "), 1)

    def test_build_slice_evidence_packet_and_prompt(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        retrieval = search_archive_multi(config, ["123 Sample St", "bottom sheets"], limit=5)
        windows = pack_chat_windows(config, retrieval.results, radius=2, seed_limit=2, max_windows=2, max_messages=8)
        evidence = build_slice_evidence_packet(windows, limit=8, question="What did Seth send after the address?")
        prompt = build_slice_reasoning_prompt("What did Seth send after the address?", windows, evidence)
        self.assertGreaterEqual(len(evidence), 3)
        self.assertIn("Window 1", prompt)
        self.assertIn("[1] Seth @ 2026-05-11T14:22:00Z", prompt)
        self.assertIn("bottom sheets", prompt)

    def test_repair_list_answer_from_evidence_appends_missing_explicit_item(self) -> None:
        answer = "For dinner, pick up:\n* Worcestershire Sauce [1]\n* 6 Chicken Legs [1]"
        evidence = [
            EvidenceItem("[1]", "msg-list", "chat-a", "Family logistics", "Seth", "2026-05-11T14:22:00Z", "Worcestershire Sauce; 6 Chicken Legs; Frozen Pizzas", 10.0)
        ]
        repaired = _repair_list_answer_from_evidence("What did she ask me to pick up from the store?", answer, evidence)
        self.assertIn("Frozen Pizzas [1]", repaired)

    def test_repair_address_answer_from_retrieval_extracts_address_when_model_misses(self) -> None:
        retrieval = type("R", (), {
            "results": [
                type("Item", (), {
                    "message_id": "msg-1",
                    "text": "Olá Ana\nDeixo a morada da nossa sede: 456 Example Ave\nObrigada\nTeresa Roque\nSample Cafe - Grp Mainside",
                    "sender_name": "Alex Morgan",
                    "chat_name": "Alex Morgan",
                    "score": 66.3,
                })()
            ]
        })()
        evidence = [
            EvidenceItem("[1]", "msg-1", "chat-a", "Alex Morgan", "Alex Morgan", "2026-05-13T10:30:36.000Z", "456 Example Ave", 66.3)
        ]
        repaired = _repair_address_answer_from_retrieval(
            "What address was sent for Sample Cafe?",
            "The provided evidence does not contain an address for Sample Cafe.",
            retrieval,
            evidence,
        )
        self.assertIn("456 Example Ave", repaired)
        self.assertIn("[1]", repaired)
        self.assertIn("[1]", repaired)

    def test_ask_archive_uses_llm_and_evidence(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "What address did Seth send?",
            llm_client=FakeLlmClient("Seth sent 123 Sample St [1]."),
        )
        self.assertEqual(response.answer, "Seth sent 123 Sample St [1].")
        self.assertEqual(response.evidence[0].sender_name, "Seth")

    def test_ask_archive_with_no_evidence(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "What is the serial number of the spaceship?",
            llm_client=FakeLlmClient(
                "unused",
                plan=QueryPlan(
                    normalized_question="What is the serial number of the spaceship?",
                    search_queries=["spaceship serial number", "serial number spaceship"],
                    answer_kind="fact",
                    time_hint="any",
                ),
            ),
        )
        self.assertEqual(response.evidence, [])
        self.assertIn("could not find enough local evidence", response.answer.lower())

    def test_ask_archive_can_answer_directly_from_memory_facts(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "Who is Alex again?",
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
        )
        self.assertEqual(response.answer, "Alex Morgan is your sister.")
        self.assertEqual(response.evidence, [])

    def test_ask_archive_can_return_alias_confirmation_directly(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "Remember that Addy is Jordan Lee.",
        )
        self.assertIn("Please confirm before I save it.", response.answer)
        self.assertIn("Addy → Jordan Lee", response.answer)
        self.assertEqual(response.evidence, [])
        self.assertEqual(response.answer_path, "direct")
        self.assertEqual(response.proposed_action["kind"], "add-alias")
        self.assertEqual(response.proposed_action["alias"], "Addy")
        self.assertEqual(response.proposed_action["canonical_name"], "Jordan Lee")

    def test_ask_archive_proposes_relationship_fact_not_alias(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "Remember that Alex is my sister.",
        )
        self.assertIn("Please confirm before I save it.", response.answer)
        self.assertNotIn("alias", response.answer)
        self.assertEqual(response.answer_path, "direct")
        self.assertEqual(response.proposed_action["kind"], "add-relationship-fact")
        self.assertEqual(response.proposed_action["subject"], "Alex")
        self.assertEqual(response.proposed_action["relationship"], "sister")

    def test_ask_archive_uses_planner_resolved_question_for_followups(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "What address did she send?",
            control_turns=[
                {"role": "user", "content": "What address did Seth send?"},
                {"role": "assistant", "content": "Seth sent 123 Sample St [1]."},
            ],
            llm_client=FakeLlmClient(
                "Seth sent 123 Sample St [1].",
                plan=QueryPlan(
                    normalized_question="",
                    search_queries=[],
                    preferred_senders=["Seth"],
                    answer_kind="fact",
                    time_hint="any",
                    resolved_question="What address did Seth send?",
                ),
            ),
        )
        self.assertEqual(response.answer, "Seth sent 123 Sample St [1].")
        self.assertEqual(response.answer_path, "model")
        self.assertEqual(response.evidence[0].sender_name, "Seth")

    def test_ask_archive_answers_memory_question_through_model(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "How is Anna related to me?",
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
            llm_client=FakeLlmClient(
                "Anna is your sister.",
                plan=QueryPlan(
                    normalized_question="How is Anna related to me?",
                    search_queries=["zz-no-archive-match-zz"],
                    answer_kind="fact",
                    time_hint="any",
                ),
            ),
        )
        self.assertEqual(response.answer, "Anna is your sister.")
        self.assertEqual(response.answer_path, "model")

    def test_llm_base_url_must_be_loopback(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)
        config.llm.base_url = "http://192.168.1.10:8085/v1"
        with self.assertRaises(LlmError):
            ask_archive(config, "What address did Seth send?")


if __name__ == "__main__":
    unittest.main()
