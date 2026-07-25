from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

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

    def test_zip_rejects_posix_traversal_and_backslash_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self._config(root)
            approve_chat(config, "wa:bad", "Bad")
            text = "[7/24/26, 9:00 AM] Alice: hello"
            for filename in ("../chat.txt", "folder\\chat.txt"):
                archive_path = root / (hashlib.sha256(filename.encode()).hexdigest() + ".zip")
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(filename, text)
                with self.subTest(filename=filename), self.assertRaisesRegex(ConfigError, "traversal"):
                    import_whatsapp(config, archive_path, "wa:bad")

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
