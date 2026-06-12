from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import MessagePage
from beeper_bot.config import load_config
from beeper_bot.retrieval import (
    detect_query_features,
    expand_results_with_context,
    expand_results_with_spans,
    format_find_response,
    pack_chat_windows,
    search_archive,
    search_archive_multi,
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


class RetrievalTest(unittest.TestCase):
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

    def _config_with_data(self) -> tuple[object, tempfile.TemporaryDirectory[str]]:
        tmpdir = tempfile.TemporaryDirectory()
        config = load_config(self._write_config(tmpdir.name, ["chat-a", "chat-b"]))
        client = FakeBeeperClient(
            chats={
                "chat-a": {"title": "Family logistics"},
                "chat-b": {"title": "Friends"},
            },
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
                        "text": "Call me at 503-555-1212 tomorrow.",
                    },
                    {
                        "id": "msg-4",
                        "sortKey": "3",
                        "timestamp": "2026-05-12T09:05:00Z",
                        "senderID": "u1",
                        "senderName": "Seth",
                        "type": "TEXT",
                        "text": "The key box code is 56890 and check in starts from 2pm onwards.",
                    },
                ],
                "chat-b": [
                    {
                        "id": "msg-3",
                        "sortKey": "3",
                        "timestamp": "2026-05-13T08:00:00Z",
                        "senderID": "u3",
                        "senderName": "Alex",
                        "type": "TEXT",
                        "text": "Website is https://example.org and email alex@example.org",
                    }
                ],
            },
        )
        sync_chats(config, client)
        return config, tmpdir

    def test_detect_query_features(self) -> None:
        self.assertEqual(detect_query_features("123 Sample St"), ["address"])
        self.assertEqual(detect_query_features("503-555-1212"), ["phone"])
        self.assertEqual(detect_query_features("alex@example.org"), ["email"])

    def test_search_archive_finds_address(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive(config, "123 Sample St")
        self.assertGreaterEqual(len(response.results), 1)
        self.assertEqual(response.results[0].message_id, "msg-1")
        self.assertIn("address-shape", response.results[0].match_reasons)

    def test_search_archive_finds_phone(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive(config, "503-555-1212")
        self.assertGreaterEqual(len(response.results), 1)
        self.assertEqual(response.results[0].message_id, "msg-2")
        self.assertIn("phone-shape", response.results[0].match_reasons)

    def test_format_find_response(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive(config, "alex@example.org")
        text = format_find_response(response)
        self.assertIn("Top matches for: alex@example.org", text)
        self.assertIn("Friends", text)
        self.assertIn("alex@example.org", text)

    def test_expand_results_with_context_keeps_message_metadata(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive(config, "503-555-1212")
        expanded = expand_results_with_context(config, response.results[:1], window=1)

        self.assertEqual([item.message_id for item in expanded], ["msg-2"])
        self.assertEqual(expanded[0].sender_name, "Julie")
        self.assertEqual(len(expanded[0].context_before), 1)
        self.assertIn("Seth @ 2026-05-11T14:22:00Z", expanded[0].context_before[0])
        self.assertNotIn("[context]", expanded[0].text)
        self.assertNotIn("[match]", expanded[0].text)

    def test_search_archive_multi_can_restrict_sender(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive_multi(
            config,
            ["Julie"],
            limit=5,
            restrict_senders=["Julie"],
        )
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].sender_name, "Julie")
        self.assertEqual(response.results[0].message_id, "msg-2")

    def test_search_archive_multi_can_apply_global_date_bounds(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive_multi(
            config,
            ["Seth", "address", "check in"],
            limit=10,
            date_start="2026-05-12T00:00:00Z",
            date_end="2026-05-12T23:59:59Z",
        )
        self.assertTrue(response.results)
        self.assertTrue(all(item.timestamp.startswith("2026-05-12") for item in response.results))

    def test_expand_results_with_spans_adds_nearby_messages(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive(config, "503-555-1212")
        expanded = expand_results_with_spans(config, "What was the key box code?", response.results[:1], window=2)

        self.assertTrue(any(item.message_id == "msg-4" for item in expanded))
        msg4 = next(item for item in expanded if item.message_id == "msg-4")
        self.assertIn("span-nearby", msg4.match_reasons)

    def test_pack_chat_windows_merges_overlapping_seed_ranges(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive_multi(config, ["123 Sample St", "key box code"], limit=5)
        windows = pack_chat_windows(config, response.results, radius=1, seed_limit=2, max_windows=2, max_messages=10)

        self.assertEqual(len(windows), 1)
        self.assertEqual([item.message_id for item in windows[0].messages], ["msg-1", "msg-2", "msg-4"])
        self.assertEqual(set(windows[0].seed_message_ids), {"msg-1", "msg-4"})

    def test_search_archive_empty_query(self) -> None:
        config, tmpdir = self._config_with_data()
        self.addCleanup(tmpdir.cleanup)

        response = search_archive(config, "   ")
        self.assertEqual(response.results, [])


if __name__ == "__main__":
    unittest.main()
