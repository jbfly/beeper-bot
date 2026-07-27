from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from beeper_bot.config import load_config
from beeper_bot.db import open_db
from beeper_bot.offline_archive import approve_chat

SPEC = importlib.util.spec_from_file_location(
    "whatsapp_export_dropbox", Path(__file__).parents[1] / "scripts" / "whatsapp_export_dropbox.py"
)
assert SPEC and SPEC.loader
DROPBOX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DROPBOX)


class WhatsAppExportDropboxTest(unittest.TestCase):
    def _paths(self, temporary: str) -> tuple[Path, Path, Path]:
        base = Path(temporary)
        config = base / "config.toml"
        config.write_text(f'[archive]\npath = "{base / "state" / "archive.sqlite3"}"\n')
        os.chmod(config, 0o600)
        manifest = base / "chats.tsv"
        manifest.write_text("folder\tchat_id\tname\nSynthetic Chat\twa:synthetic\tSynthetic Chat\n")
        os.chmod(manifest, 0o600)
        root = base / "WhatsApp Exports"
        DROPBOX.setup(root, manifest)
        approve_chat(load_config(config), "wa:synthetic", "Synthetic Chat")
        return root, config, root / "Synthetic Chat"

    def _export(self, folder: Path, name: str = "export.txt", text: str | None = None) -> Path:
        path = folder / name
        path.write_text(text or "[27/07/26, 09:00] Person One: Synthetic alpha.\n[27/07/26, 09:01] Person Two: Synthetic beta.\n")
        os.chmod(path, 0o600)
        return path

    def test_success_moves_export_and_writes_metadata_only_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, folder = self._paths(temporary)
            source = self._export(folder, "chat export.txt")
            result = DROPBOX.scan(root, config)
            self.assertEqual(result, {"imported": 2, "duplicates": 0, "failed": 0, "files": 1})
            self.assertFalse(source.exists())
            receipt_path = next((root / "Processed").rglob("receipt.json"))
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual((receipt["chat_id"], receipt["chat_name"], receipt["filename"]),
                             ("wa:synthetic", "Synthetic Chat", "chat export.txt"))
            self.assertNotIn("Synthetic alpha", receipt_path.read_text())

    def test_rerun_of_same_export_adds_no_duplicate_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, folder = self._paths(temporary)
            content = "[27/07/26, 09:00] Person One: Synthetic repeat.\n"
            self._export(folder, text=content)
            self.assertEqual(DROPBOX.scan(root, config)["imported"], 1)
            self._export(folder, name="same again.txt", text=content)
            second = DROPBOX.scan(root, config)
            self.assertEqual((second["imported"], second["duplicates"]), (0, 1))
            with open_db(load_config(config).archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_missing_wrong_and_duplicate_metadata_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, folder = self._paths(temporary)
            (folder / "chat.json").unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                DROPBOX.scan(root, config)
            DROPBOX._write_json(folder / "chat.json", {"chat_id": "wa:synthetic", "name": "Synthetic Chat", "extra": True})
            with self.assertRaisesRegex(ValueError, "only"):
                DROPBOX.scan(root, config)
            DROPBOX._write_json(folder / "chat.json", {"chat_id": "wa:synthetic", "name": "Synthetic Chat"})
            duplicate = root / "Other Folder"
            DROPBOX._mkdir(duplicate)
            DROPBOX._write_json(duplicate / "chat.json", {"chat_id": "wa:synthetic", "name": "Other"})
            with self.assertRaisesRegex(ValueError, "duplicate chat_id"):
                DROPBOX.scan(root, config)

    def test_symlink_source_is_refused_and_left_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, folder = self._paths(temporary)
            target = Path(temporary) / "outside.txt"
            target.write_text("[27/07/26, 09:00] Person: Secret outside text.\n")
            source = folder / "linked.txt"
            source.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink|escapes"):
                DROPBOX.scan(root, config)
            self.assertTrue(source.is_symlink())
            source.unlink()
            destination_link = root / "Processed" / "Synthetic Chat"
            destination_link.symlink_to(Path(temporary))
            source = self._export(folder)
            with self.assertRaisesRegex(ValueError, "escapes|symlink"):
                DROPBOX.scan(root, config)
            self.assertTrue(source.exists())

    def test_failure_receipt_and_cli_error_never_include_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, folder = self._paths(temporary)
            secret = "PRIVATE SOURCE SENTENCE MUST NOT LEAK"
            self._export(folder, text=secret)
            result = DROPBOX.scan(root, config)
            self.assertEqual(result["failed"], 1)
            receipt_path = next((root / "Failed").rglob("receipt.json"))
            receipt_text = receipt_path.read_text()
            self.assertIn("import failed (ConfigError)", receipt_text)
            self.assertNotIn(secret, receipt_text)

    def test_setup_outputs_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, folder = self._paths(temporary)
            self._export(folder)
            DROPBOX.scan(root, config)
            paths = [root, folder, root / "Processed", root / "Failed",
                     folder / "chat.json", next((root / "Processed").rglob("receipt.json")),
                     load_config(config).archive.path]
            for path in paths:
                expected = 0o700 if path.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path))


if __name__ == "__main__":
    unittest.main()
