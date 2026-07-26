from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import beeper_bot.media as media_mod
from beeper_bot.beeper_api import MessagePage
from beeper_bot.config import load_config
from beeper_bot.offline_archive import approve_chat
from beeper_bot.db import open_db
from beeper_bot.llm import ask_archive
from beeper_bot.media import (
    derive_message_media,
    find_voice_transcripts,
    parse_memo_request,
    pending_media_messages,
    run_derivation_pass,
)
from beeper_bot.retrieval import search_archive
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


class FakeMediaClient:
    def __init__(self):
        self.audio_calls = 0
        self.image_calls = 0

    def transcribe_audio_wav(self, config, wav_bytes: bytes) -> str:
        self.audio_calls += 1
        return f"chunk {self.audio_calls}: remember to pick up the olive oil"

    def describe_image(self, config, image_bytes: bytes, mime_type: str) -> str:
        self.image_calls += 1
        return 'A handwritten note reading "MEET AT PIER 39".'


def _voice_message(idx: int, src_url: str) -> dict:
    return {
        "id": f"voice-{idx}",
        "sortKey": str(idx),
        "timestamp": f"2026-06-{idx:02d}T10:00:00Z",
        "senderID": "u1",
        "senderName": "Jordan Lee",
        "isSender": True,
        "type": "VOICE",
        "attachments": [
            {
                "id": f"mxc://local/voice-{idx}",
                "fileName": "Voice message.ogg",
                "mimeType": "audio/ogg; codecs=opus",
                "isVoiceNote": True,
                "type": "audio",
                "srcURL": src_url,
            }
        ],
    }


def _image_message(idx: int, src_url: str) -> dict:
    return {
        "id": f"img-{idx}",
        "sortKey": str(100 + idx),
        "timestamp": f"2026-06-{idx:02d}T11:00:00Z",
        "senderID": "u2",
        "senderName": "Alex Morgan",
        "type": "IMAGE",
        "attachments": [
            {
                "id": f"mxc://local/img-{idx}",
                "fileName": "photo.jpg",
                "mimeType": "image/jpeg",
                "type": "img",
                "srcURL": src_url,
            }
        ],
    }


class MediaTestBase(unittest.TestCase):
    def _config_with_media(self):
        tmpdir = tempfile.TemporaryDirectory()
        config_path = Path(tmpdir.name) / "config.toml"
        db_path = Path(tmpdir.name) / "archive.sqlite3"
        config_path.write_text(f'[archive]\npath = "{db_path}"\n\n[beeper]\nindexed_chat_ids = ["chat-a"]\n')
        config = load_config(config_path)

        fake_audio = Path(tmpdir.name) / "memo.ogg"
        fake_audio.write_bytes(b"fake-ogg-data")
        fake_image = Path(tmpdir.name) / "photo.jpg"
        fake_image.write_bytes(b"fake-jpeg-data")

        client = FakeBeeperClient(
            chats={"chat-a": {"title": "Jordan Lee"}},
            messages={
                "chat-a": [
                    _voice_message(1, f"file://{fake_audio}"),
                    _image_message(2, f"file://{fake_image}"),
                ]
            },
        )
        for chat_id in client.chats:
            approve_chat(config, chat_id, chat_id)
        sync_chats(config, client)
        return config, client, tmpdir

    def setUp(self) -> None:
        # 60s fake duration -> 3 chunks at 28s with 2s overlap (offsets 0/26/52)
        self._orig_duration = media_mod._ffprobe_duration
        self._orig_chunk = media_mod._audio_chunk_wav
        media_mod._ffprobe_duration = lambda path: 60.0
        media_mod._audio_chunk_wav = lambda path, offset, length: b"fake-wav"

    def tearDown(self) -> None:
        media_mod._ffprobe_duration = self._orig_duration
        media_mod._audio_chunk_wav = self._orig_chunk


class MediaTest(MediaTestBase):
    def test_voice_memo_is_transcribed_chunked_and_indexed(self) -> None:
        config, client, tmpdir = self._config_with_media()
        self.addCleanup(tmpdir.cleanup)
        fake = FakeMediaClient()
        results = run_derivation_pass(config, "voice-memo", limit=10, llm_client=fake)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.status, "done")
        self.assertEqual(result.chunk_count, 3)
        self.assertEqual(fake.audio_calls, 3)
        self.assertIn("[voice memo transcript]", result.derived_text)
        self.assertIn("olive oil", result.derived_text)

        response = search_archive(config, "olive oil")
        self.assertGreaterEqual(len(response.results), 1)
        self.assertEqual(response.results[0].message_id, "voice-1")
        self.assertEqual(response.results[0].sender_name, "Jordan Lee")

    def test_image_description_is_indexed(self) -> None:
        config, client, tmpdir = self._config_with_media()
        self.addCleanup(tmpdir.cleanup)
        fake = FakeMediaClient()
        results = run_derivation_pass(config, "image", limit=10, llm_client=fake)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "done")
        self.assertIn("PIER 39", results[0].derived_text)

        response = search_archive(config, "PIER 39")
        self.assertGreaterEqual(len(response.results), 1)
        self.assertEqual(response.results[0].message_id, "img-2")

    def test_captioned_image_keeps_caption_and_placeholder_after_description(self) -> None:
        config, client, tmpdir = self._config_with_media()
        self.addCleanup(tmpdir.cleanup)
        client.messages["chat-a"][1]["text"] = "Receipt from the hardware store"
        sync_chats(config, client)

        with open_db(config.archive.path) as conn:
            synced = str(conn.execute("SELECT text FROM messages WHERE message_id = 'img-2'").fetchone()["text"])
        self.assertIn("Receipt from the hardware store", synced)
        self.assertIn("[image: photo.jpg]", synced)

        run_derivation_pass(config, "image", limit=10, llm_client=FakeMediaClient())
        with open_db(config.archive.path) as conn:
            indexed = str(conn.execute("SELECT text FROM messages WHERE message_id = 'img-2'").fetchone()["text"])
            searchable = str(conn.execute("SELECT text FROM message_fts WHERE message_id = 'img-2'").fetchone()["text"])
        for text in (indexed, searchable):
            self.assertIn("Receipt from the hardware store", text)
            self.assertIn("[image: photo.jpg]", text)
            self.assertIn("PIER 39", text)

    def test_derived_text_survives_resync(self) -> None:
        config, client, tmpdir = self._config_with_media()
        self.addCleanup(tmpdir.cleanup)
        run_derivation_pass(config, "voice-memo", limit=10, llm_client=FakeMediaClient())

        for chat_id in client.chats:
            approve_chat(config, chat_id, chat_id)
        sync_chats(config, client)

        with open_db(config.archive.path) as conn:
            row = conn.execute("SELECT text FROM messages WHERE message_id = 'voice-1'").fetchone()
        self.assertIn("olive oil", str(row["text"]))
        response = search_archive(config, "olive oil")
        self.assertEqual(response.results[0].message_id, "voice-1")

    def test_excluded_chats_are_never_processed(self) -> None:
        config, client, tmpdir = self._config_with_media()
        self.addCleanup(tmpdir.cleanup)
        config.media.exclude_chat_ids = ["chat-a"]
        self.assertEqual(pending_media_messages(config, "voice-memo", limit=10), [])
        self.assertEqual(run_derivation_pass(config, "voice-memo", limit=10, llm_client=FakeMediaClient()), [])

    def test_processed_messages_are_not_reprocessed(self) -> None:
        config, client, tmpdir = self._config_with_media()
        self.addCleanup(tmpdir.cleanup)
        fake = FakeMediaClient()
        run_derivation_pass(config, "voice-memo", limit=10, llm_client=fake)
        self.assertEqual(pending_media_messages(config, "voice-memo", limit=10), [])
        second = run_derivation_pass(config, "voice-memo", limit=10, llm_client=fake)
        self.assertEqual(second, [])
        self.assertEqual(fake.audio_calls, 3)

    def test_failed_download_is_recorded_not_raised(self) -> None:
        config, client, tmpdir = self._config_with_media()
        self.addCleanup(tmpdir.cleanup)
        raw = json.dumps(_voice_message(9, "file:///nonexistent/missing.ogg"))
        result = derive_message_media(config, "voice-9", "chat-a", raw, "voice-memo", FakeMediaClient())
        self.assertEqual(result.status, "failed")
        self.assertIn("missing", result.error_text)


class FakeSummarizer:
    def __init__(self, summary: str = "Summary: a reminder about olive oil."):
        self.summary = summary
        self.prompts: list[str] = []

    def summarize_text(self, config, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.summary


class MemoRequestTest(unittest.TestCase):
    def test_parse_transcript_and_summary_shapes(self) -> None:
        req = parse_memo_request("Can you give me a transcript of my most recent voice memo?")
        self.assertEqual(req.action, "transcript")
        self.assertTrue(req.mine_only)

        req = parse_memo_request("Summarize the 21 min memo")
        self.assertEqual(req.action, "summary")
        self.assertEqual(req.duration_minutes, 21)
        self.assertFalse(req.mine_only)

        req = parse_memo_request("Transcribe the last voice note from Jordan")
        self.assertEqual(req.action, "transcript")
        self.assertEqual(req.sender_query, "Jordan")

    def test_parse_ignores_non_memo_questions(self) -> None:
        self.assertIsNone(parse_memo_request("What address did Taylor send?"))
        self.assertIsNone(parse_memo_request("Summarize the Neighborhood chat"))
        self.assertIsNone(parse_memo_request("What did Anna say about the memo?"))


class MemoLookupTest(MediaTestBase):
    def _derived(self):
        config, client, tmpdir = self._config_with_media()
        run_derivation_pass(config, "voice-memo", limit=10, llm_client=FakeMediaClient())
        return config, tmpdir

    def test_find_voice_transcripts_filters_by_duration(self) -> None:
        config, tmpdir = self._derived()
        self.addCleanup(tmpdir.cleanup)
        self.assertEqual(len(find_voice_transcripts(config)), 1)
        # fake duration is 60s: a 1-minute filter matches, a 10-minute one does not
        self.assertEqual(len(find_voice_transcripts(config, duration_minutes=1)), 1)
        self.assertEqual(find_voice_transcripts(config, duration_minutes=10), [])

    def test_ask_returns_full_transcript_directly(self) -> None:
        config, tmpdir = self._derived()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "Give me the transcript of the latest voice memo.",
            llm_client=FakeSummarizer(),
            control_turns=[],
            memory_state={},
        )
        self.assertEqual(response.answer_path, "direct")
        self.assertIn("Voice memo from Jordan Lee", response.answer)
        self.assertIn("olive oil", response.answer)
        self.assertNotIn("[voice memo transcript]", response.answer)

    def test_ask_summarizes_memo_with_full_transcript(self) -> None:
        config, tmpdir = self._derived()
        self.addCleanup(tmpdir.cleanup)
        summarizer = FakeSummarizer()
        response = ask_archive(
            config,
            "Summarize my last voice memo",
            llm_client=summarizer,
            control_turns=[],
            memory_state={},
        )
        self.assertEqual(response.answer_path, "model")
        self.assertIn("Summary: a reminder about olive oil.", response.answer)
        # the summarizer must have seen the whole transcript, not an excerpt
        self.assertIn("chunk 3", summarizer.prompts[0])

    def test_ask_reports_no_matching_memos(self) -> None:
        config, tmpdir = self._derived()
        self.addCleanup(tmpdir.cleanup)
        response = ask_archive(
            config,
            "Transcribe the voice memo from Wolfgang",
            llm_client=FakeSummarizer(),
            control_turns=[],
            memory_state={},
        )
        self.assertEqual(response.answer_path, "direct")
        self.assertIn("no transcribed voice memos", response.answer)
        self.assertIn("from Wolfgang", response.answer)


if __name__ == "__main__":
    unittest.main()
