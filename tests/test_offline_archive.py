from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from beeper_bot.config import ConfigError, load_config
from beeper_bot.db import open_db
from beeper_bot.sync import _upsert_chat
from beeper_bot.offline_archive import (
    approve_chat,
    import_whatsapp,
    list_approved_chats,
    revoke_chat,
    scoped_search,
    surrounding_thread,
)


class OfflineArchiveTest(unittest.TestCase):
    def test_approved_zip_import_is_idempotent_cited_and_revocable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.toml"
            config_path.write_text(f'[archive]\npath = "{root / "archive.sqlite3"}"\n')
            config = load_config(config_path)
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
            first = import_whatsapp(config, export, "wa:test")
            second = import_whatsapp(config, export, "wa:test")
            self.assertEqual(first["message_count"], 2)
            self.assertEqual(second["message_count"], 2)
            self.assertEqual(list_approved_chats(config)[0]["chat_id"], "wa:test")

            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0], 2)
                raw = json.loads(conn.execute("SELECT raw_json FROM messages ORDER BY sort_key LIMIT 1").fetchone()[0])
                self.assertNotIn("cedar gate", json.dumps(raw))

            results = scoped_search(config, "wa:test", "blue key")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["sender"], "Bob")
            self.assertEqual(results[0]["citation"]["source"]["kind"], "whatsapp-export")
            self.assertIn("#L2", results[0]["citation"]["source"]["ref"])
            thread = surrounding_thread(config, "wa:test", results[0]["citation"]["message_id"], radius=1_000)
            self.assertEqual([item["sender"] for item in thread], ["Alice", "Bob"])
            self.assertIn("continued on a second line", thread[1]["text"])

            self.assertTrue(revoke_chat(config, "wa:test"))
            self.assertEqual(scoped_search(config, "wa:test", "blue key"), [])
            self.assertEqual(surrounding_thread(config, "wa:test", results[0]["citation"]["message_id"]), [])
            with open_db(config.archive.path) as conn:
                _upsert_chat(conn, "wa:test", "Renamed by live sync")
                conn.commit()
            self.assertEqual(scoped_search(config, "wa:test", "blue key"), [])

            bad_zip = root / "bad.zip"
            with zipfile.ZipFile(bad_zip, "w") as archive:
                archive.writestr("../chat.txt", chat_text)
            approve_chat(config, "wa:bad", "Bad")
            with self.assertRaisesRegex(ConfigError, "traversal"):
                import_whatsapp(config, bad_zip, "wa:bad")


if __name__ == "__main__":
    unittest.main()
