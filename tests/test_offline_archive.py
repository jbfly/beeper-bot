from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import beeper_bot.offline_archive as archive_mod

from beeper_bot.cli import main as cli_main
from beeper_bot.config import ConfigError, load_config
from beeper_bot.db import open_db
from beeper_bot.offline_archive import (
    approve_chat,
    import_whatsapp,
    list_approved_chats,
    revoke_chat,
    scoped_search,
    surrounding_thread,
)
from beeper_bot.sync import _upsert_chat, _upsert_message


class OfflineArchiveTest(unittest.TestCase):
    def _config(self, root: Path):
        config_path = root / "config.toml"
        config_path.write_text(f'[archive]\npath = "{root / "archive.sqlite3"}"\n')
        return load_config(config_path)

    def test_import_keeps_both_sources_cited_and_revocation_survives_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            export = root / "chat.zip"
            chat_text = "\n".join([
                "[7/24/26, 9:00:00 AM] Alice: Meet at the cedar gate.",
                "[7/24/26, 9:01:00 AM] Bob: I will bring the blue key.",
                "continued on a second line",
                "[7/24/26, 9:02:00 AM] Messages are end-to-end encrypted.",
            ])
            with zipfile.ZipFile(export, "w") as archive:
                archive.writestr("WhatsApp Chat with Test Group.txt", chat_text)

            with self.assertRaisesRegex(ConfigError, "not approved"):
                import_whatsapp(config, export, "wa:test")

            approve_chat(config, "wa:test", "Test Group")
            with open_db(config.archive.path) as conn:
                _upsert_message(conn, "wa:test", "Test Group", {
                    "id": "live-bob", "sortKey": 42, "timestamp": "2026-07-24T09:01:00Z",
                    "senderID": "matrix-bob", "senderName": "Bob", "type": "TEXT",
                    "text": "I will bring the blue key.\ncontinued on a second line",
                })
                conn.commit()

            first = import_whatsapp(config, export, "wa:test")
            second = import_whatsapp(config, export, "wa:test")
            self.assertEqual(first["message_count"], 2)
            self.assertEqual(second["message_count"], 2)
            self.assertEqual(first["source_artifact_sha256"], hashlib.sha256(export.read_bytes()).hexdigest())
            self.assertEqual(list_approved_chats(config)[0]["chat_id"], "wa:test")

            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0], 3)
                imported = conn.execute(
                    """SELECT raw_json, source_artifact_sha256, evidence_fingerprint, possible_duplicate_of
                       FROM messages WHERE source_kind = 'whatsapp-export' AND sender_name = 'Bob'"""
                ).fetchone()
                live = conn.execute(
                    "SELECT source_artifact_sha256, evidence_fingerprint FROM messages WHERE message_id = 'live-bob'"
                ).fetchone()
                self.assertNotIn("blue key", imported["raw_json"])
                self.assertEqual(len(imported["source_artifact_sha256"]), 64)
                self.assertEqual(len(live["source_artifact_sha256"]), 64)
                self.assertEqual(imported["evidence_fingerprint"], live["evidence_fingerprint"])
                self.assertEqual(imported["possible_duplicate_of"], "live-bob")

                approval_before = tuple(conn.execute(
                    "SELECT is_allowed, approval_source, approved_at, revoked_at FROM chats WHERE chat_id = 'wa:test'"
                ).fetchone())
                _upsert_chat(conn, "wa:test", "Renamed by live sync")
                conn.commit()
                approval_after = tuple(conn.execute(
                    "SELECT is_allowed, approval_source, approved_at, revoked_at FROM chats WHERE chat_id = 'wa:test'"
                ).fetchone())
                self.assertEqual(approval_after, approval_before)
                self.assertEqual(
                    {row[0] for row in conn.execute("SELECT chat_name FROM message_fts WHERE chat_id = 'wa:test'")},
                    {"Renamed by live sync"},
                )

            results = scoped_search(config, "wa:test", "blue key")
            self.assertEqual(len(results), 2)
            self.assertEqual({item["citation"]["source"]["kind"] for item in results}, {"beeper", "whatsapp-export"})
            imported_result = next(item for item in results if item["citation"]["source"]["kind"] == "whatsapp-export")
            self.assertEqual(imported_result["citation"]["source"]["possible_duplicate_of"], "live-bob")
            thread = surrounding_thread(config, "wa:test", imported_result["citation"]["message_id"], radius=50)
            self.assertTrue(any(item["sender"] == "Alice" for item in thread))

            self.assertTrue(revoke_chat(config, "wa:test"))
            with open_db(config.archive.path) as conn:
                revoked_before = tuple(conn.execute(
                    "SELECT is_allowed, approval_source, approved_at, revoked_at FROM chats WHERE chat_id = 'wa:test'"
                ).fetchone())
                _upsert_chat(conn, "wa:test", "Another live name")
                conn.commit()
                revoked_after = tuple(conn.execute(
                    "SELECT is_allowed, approval_source, approved_at, revoked_at FROM chats WHERE chat_id = 'wa:test'"
                ).fetchone())
            self.assertEqual(revoked_after, revoked_before)
            self.assertEqual(scoped_search(config, "wa:test", "blue key"), [])
            self.assertEqual(surrounding_thread(config, "wa:test", imported_result["citation"]["message_id"]), [])

    def test_media_extraction_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            approve_chat(config, "wa:default", "Default")
            export = root / "chat.zip"
            with zipfile.ZipFile(export, "w") as archive:
                archive.writestr("chat.txt", "[24/7/26, 09:00] Alice: <attached: photo.jpg>")
                archive.writestr("photo.jpg", b"image")
            result = import_whatsapp(config, export, "wa:default", date_order="day-first")
            self.assertEqual((result["media_extracted"], result["media_skipped_video"], result["media_failed"]),
                             (0, 0, 0))
            self.assertFalse((root / "media").exists())

    def test_media_extracts_documents_and_images_skips_video_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            approve_chat(config, "wa:media", "Media")
            export = root / "chat.zip"
            with zipfile.ZipFile(export, "w") as archive:
                archive.writestr("chat.txt", "[24/7/26, 09:00] Alice: <attached: photo.jpg>")
                archive.writestr("nested/photo.jpg", b"image")
                archive.writestr("report.pdf", b"document")
                archive.writestr("clip.MP4", b"video")
            first = import_whatsapp(config, export, "wa:media", date_order="day-first", extract_media=True)
            second = import_whatsapp(config, export, "wa:media", date_order="day-first", extract_media=True)
            self.assertEqual((first["media_extracted"], first["media_skipped_video"], first["media_failed"]),
                             (2, 1, 0))
            self.assertEqual((second["media_extracted"], second["media_skipped_video"], second["media_failed"]),
                             (0, 1, 0))
            media_dir = root / "media" / "wa:media"
            self.assertEqual({path.name: path.read_bytes() for path in media_dir.iterdir()},
                             {"photo.jpg": b"image", "report.pdf": b"document"})
            self.assertEqual(stat.S_IMODE((root / "media").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(media_dir.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in media_dir.iterdir()))

            changed = root / "changed.zip"
            with zipfile.ZipFile(changed, "w") as archive:
                archive.writestr("chat.txt", "[24/7/26, 09:01] Alice: <attached: photo.jpg>")
                archive.writestr("photo.jpg", b"different image")
            collision = import_whatsapp(config, changed, "wa:media", date_order="day-first", extract_media=True)
            self.assertEqual(collision["media_extracted"], 1)
            names = {path.name for path in media_dir.iterdir()}
            self.assertIn("photo.jpg", names)
            self.assertTrue(any(name.startswith("photo-") and name.endswith(".jpg") for name in names))
            again = import_whatsapp(config, changed, "wa:media", date_order="day-first", extract_media=True)
            self.assertEqual(again["media_extracted"], 0)

    def test_media_failure_does_not_fail_text_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            approve_chat(config, "wa:failure", "Failure")
            export = root / "chat.zip"
            with zipfile.ZipFile(export, "w") as archive:
                archive.writestr("chat.txt", "[24/7/26, 09:00] Alice: files attached")
                archive.writestr("photo.jpg", b"image")
                archive.writestr("report.pdf", b"document")
            original = archive_mod._write_media_member
            calls = 0

            def fail_second(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic failure with private details")
                return original(*args)

            with mock.patch.object(archive_mod, "_write_media_member", side_effect=fail_second):
                result = import_whatsapp(config, export, "wa:failure", date_order="day-first", extract_media=True)
            self.assertEqual((result["message_count"], result["media_extracted"], result["media_failed"]), (1, 1, 1))
            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 'wa:failure'").fetchone()[0], 1)

    def test_zip_security_guards_run_before_media_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            approve_chat(config, "wa:bad", "Bad")
            text = "[24/7/26, 09:00] Alice: hello"
            cases: list[tuple[str, zipfile.ZipInfo | str]] = [
                ("traversal", "../photo.jpg"),
                ("absolute", "/photo.jpg"),
                ("backslash", "folder\\photo.jpg"),
            ]
            symlink = zipfile.ZipInfo("photo.jpg")
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            cases.append(("symlink", symlink))
            for label, bad_member in cases:
                archive_path = root / f"{label}.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("chat.txt", text)
                    archive.writestr(bad_member, b"outside")
                with self.subTest(label=label), self.assertRaises(ConfigError):
                    import_whatsapp(config, archive_path, "wa:bad", date_order="day-first", extract_media=True)
            self.assertFalse((root / "media").exists())
            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_zip_expanded_limit_refuses_before_media_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            approve_chat(config, "wa:large", "Large")
            export = root / "large.zip"
            with zipfile.ZipFile(export, "w") as archive:
                archive.writestr("chat.txt", "[24/7/26, 09:00] Alice: file attached")
                archive.writestr("report.pdf", b"document")
            with mock.patch.object(archive_mod, "MAX_ZIP_EXPANDED_BYTES", 10), \
                    self.assertRaisesRegex(ConfigError, "size limit"):
                import_whatsapp(config, export, "wa:large", date_order="day-first", extract_media=True)
            self.assertFalse((root / "media").exists())

    def test_ambiguous_dates_require_explicit_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            approve_chat(config, "wa:anna", "Anna")
            export = root / "anna.txt"
            export.write_text("[07/08/26, 14:00] Anna: European date\n")
            with self.assertRaisesRegex(ConfigError, "ambiguous.*--date-order"):
                import_whatsapp(config, export, "wa:anna")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(["--config", str(config.config_path), "import-whatsapp", str(export),
                                      "--chat-id", "wa:anna", "--date-order", "day-first", "--json"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["message_count"], 1)
            with open_db(config.archive.path) as conn:
                timestamp = conn.execute("SELECT timestamp FROM messages WHERE chat_id = 'wa:anna'").fetchone()[0]
            self.assertEqual(timestamp, "2026-08-07T14:00:00")


if __name__ == "__main__":
    unittest.main()
