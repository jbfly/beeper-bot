from __future__ import annotations

import errno
import importlib.util
import json
import os
import plistlib
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

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
    def _paths(self, temporary: str) -> tuple[Path, Path, Path, Path]:
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
        return root, config, manifest, root / "Synthetic Chat"

    def _export(self, folder: Path, name: str = "export.txt", text: str | None = None,
                *, quiet: bool = True) -> Path:
        path = folder / name
        path.write_text(text or "[27/07/26, 09:00] Person One: Synthetic alpha.\n[27/07/26, 09:01] Person Two: Synthetic beta.\n")
        os.chmod(path, 0o600)
        if quiet:
            old = time.time() - DROPBOX.QUIET_SECONDS - 1
            os.utime(path, (old, old))
        return path

    def test_success_moves_export_and_writes_metadata_only_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            source = self._export(folder, "chat export.txt")
            result = DROPBOX.scan(root, config, manifest)
            self.assertEqual({key: result[key] for key in ("imported", "duplicates", "failed", "files")},
                             {"imported": 2, "duplicates": 0, "failed": 0, "files": 1})
            self.assertEqual(result["folders"]["Synthetic Chat"],
                             {"imported": 2, "duplicates": 0, "failed": 0, "files": 1, "skipped": 0,
                              "media_extracted": 0, "media_skipped_video": 0, "media_failed": 0})
            self.assertFalse(source.exists())
            receipt_path = next((root / "Processed").rglob("receipt.json"))
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual((receipt["chat_id"], receipt["chat_name"], receipt["filename"]),
                             ("wa:synthetic", "Synthetic Chat", "chat export.txt"))
            self.assertNotIn("Synthetic alpha", receipt_path.read_text())

    def test_zip_media_is_retained_outside_drop_folder_and_counted_in_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            source = folder / "export.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("chat.txt", "[27/07/26, 09:00] Person One: <attached: photo.jpg>\n")
                archive.writestr("photo.jpg", b"image")
                archive.writestr("report.pdf", b"document")
                archive.writestr("clip.3gp", b"video")
            os.chmod(source, 0o600)
            old = time.time() - DROPBOX.QUIET_SECONDS - 1
            os.utime(source, (old, old))
            result = DROPBOX.scan(root, config, manifest)
            self.assertEqual((result["media_extracted"], result["media_skipped_video"], result["media_failed"]),
                             (2, 1, 0))
            receipt_path = next((root / "Processed").rglob("receipt.json"))
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual((receipt["media_extracted"], receipt["media_skipped_video"], receipt["media_failed"]),
                             (2, 1, 0))
            self.assertNotIn("Person One", receipt_path.read_text())
            media_dir = Path(temporary) / "state" / "media" / "wa:synthetic"
            self.assertEqual({path.name for path in media_dir.iterdir()}, {"photo.jpg", "report.pdf"})
            self.assertFalse(media_dir.resolve().is_relative_to(root.resolve()))

    def test_unknown_finder_file_does_not_block_valid_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            finder_file = folder / ".DS_Store"
            finder_file.write_bytes(b"synthetic finder metadata")
            os.chmod(finder_file, 0o600)
            self._export(folder)
            result = DROPBOX.scan(root, config, manifest)
            self.assertEqual((result["imported"], result["files"]), (2, 1))
            self.assertEqual(result["folders"]["Synthetic Chat"]["skipped"], 1)
            self.assertTrue(finder_file.exists())

    def test_failed_folder_does_not_starve_later_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            manifest.write_text("folder\tchat_id\tname\nA Broken\twa:broken\tA Broken\nSynthetic Chat\twa:synthetic\tSynthetic Chat\n")
            os.chmod(manifest, 0o600)
            DROPBOX.setup(root, manifest)
            approve_chat(load_config(config), "wa:broken", "A Broken")
            target = Path(temporary) / "outside.txt"
            target.write_text("synthetic outside text")
            (root / "A Broken" / "linked.txt").symlink_to(target)
            self._export(folder)
            result = DROPBOX.scan(root, config, manifest)
            self.assertIn("error", result["folders"]["A Broken"])
            self.assertEqual(result["folders"]["Synthetic Chat"]["imported"], 2)
            self.assertEqual(result["imported"], 2)

    def test_manifest_retarget_is_refused_without_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            approve_chat(load_config(config), "wa:other", "Other Approved Chat")
            DROPBOX._write_json(folder / "chat.json", {"chat_id": "wa:other", "name": "Other Approved Chat"})
            source = self._export(folder)
            result = DROPBOX.scan(root, config, manifest)
            self.assertEqual((result["imported"], result["files"]), (0, 0))
            self.assertEqual(result["folders"]["Synthetic Chat"]["error"], "folder refused (ValueError)")
            self.assertTrue(source.exists())
            with open_db(load_config(config).archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_rerun_of_same_export_adds_no_duplicate_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            content = "[27/07/26, 09:00] Person One: Synthetic repeat.\n"
            self._export(folder, text=content)
            self.assertEqual(DROPBOX.scan(root, config, manifest)["imported"], 1)
            self._export(folder, name="same again.txt", text=content)
            second = DROPBOX.scan(root, config, manifest)
            self.assertEqual((second["imported"], second["duplicates"]), (0, 1))
            with open_db(load_config(config).archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_missing_and_wrong_metadata_are_refused_per_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            (folder / "chat.json").unlink()
            result = DROPBOX.scan(root, config, manifest)
            self.assertEqual(result["folders"]["Synthetic Chat"]["error"], "folder refused (ValueError)")
            DROPBOX._write_json(folder / "chat.json", {"chat_id": "wa:synthetic", "name": "Synthetic Chat", "extra": True})
            result = DROPBOX.scan(root, config, manifest)
            self.assertEqual(result["folders"]["Synthetic Chat"]["error"], "folder refused (ValueError)")

    def test_symlink_source_is_refused_and_left_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            target = Path(temporary) / "outside.txt"
            target.write_text("[27/07/26, 09:00] Person: Secret outside text.\n")
            source = folder / "linked.txt"
            source.symlink_to(target)
            result = DROPBOX.scan(root, config, manifest)
            self.assertIn("error", result["folders"]["Synthetic Chat"])
            self.assertTrue(source.is_symlink())
            source.unlink()
            destination_link = root / "Processed" / "Synthetic Chat"
            destination_link.symlink_to(Path(temporary))
            source = self._export(folder)
            result = DROPBOX.scan(root, config, manifest)
            self.assertIn("error", result["folders"]["Synthetic Chat"])
            self.assertTrue(source.exists())

    def test_failure_receipt_and_cli_error_never_include_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            leaked_fragment = "timestamp does not match the selected date order: 04/13/26 09:00"
            self._export(folder, text="[04/13/26, 09:00] Person: Synthetic invalid timestamp.\n")
            result = DROPBOX.scan(root, config, manifest)
            self.assertEqual(result["failed"], 1)
            receipt_path = next((root / "Failed").rglob("receipt.json"))
            receipt_text = receipt_path.read_text()
            self.assertIn("import failed (ConfigError)", receipt_text)
            self.assertNotIn(leaked_fragment, receipt_text)

    def test_new_source_waits_for_quiescence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            source = self._export(folder, quiet=False)
            first = DROPBOX.scan(root, config, manifest)
            self.assertEqual((first["imported"], first["files"]), (0, 0))
            self.assertTrue(source.exists())
            old = time.time() - DROPBOX.QUIET_SECONDS - 1
            os.utime(source, (old, old))
            self.assertEqual(DROPBOX.scan(root, config, manifest)["imported"], 2)

    def test_cross_filesystem_move_falls_back_to_shutil(self) -> None:
        source = Path("synthetic-source")
        destination = Path("synthetic-destination")
        with mock.patch.object(DROPBOX.os, "replace", side_effect=OSError(errno.EXDEV, "cross-device")), \
                mock.patch.object(DROPBOX.shutil, "move") as move:
            DROPBOX._move(source, destination)
        move.assert_called_once_with(source, destination)

    def test_launchd_preserves_shared_directory_mode_and_passes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            os.chmod(agents, 0o755)
            root = home / "WhatsApp Exports"
            config = home / "config.toml"
            manifest = home / "chats.tsv"
            script = home / "scanner.py"
            with mock.patch.object(DROPBOX.Path, "home", return_value=home):
                plist = DROPBOX.install_launchd(root, config, manifest, script)
            payload = plistlib.loads(plist.read_bytes())
            self.assertEqual(stat.S_IMODE(agents.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(plist.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((home / ".local/state/beeper-bot/whatsapp-export-dropbox.log").stat().st_mode),
                             0o600)
            self.assertEqual(payload["ProgramArguments"][-2:], ["--manifest", str(manifest)])

    def test_setup_outputs_are_private_and_accessible_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, config, manifest, folder = self._paths(temporary)
            self._export(folder)
            DROPBOX.scan(root, config, manifest)
            paths = [root, folder, root / "Processed", root / "Failed",
                     folder / "chat.json", next((root / "Processed").rglob("receipt.json")),
                     load_config(config).archive.path]
            for path in paths:
                expected = 0o700 if path.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path))
            os.chmod(root, 0o750)
            with self.assertRaises(ValueError):
                DROPBOX.scan(root, config, manifest)


if __name__ == "__main__":
    unittest.main()
