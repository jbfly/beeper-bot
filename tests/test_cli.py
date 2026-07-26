from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from beeper_bot.cli import main as cli_main
from beeper_bot.config import load_config
from beeper_bot.db import SCHEMA_VERSION, open_db
from beeper_bot.offline_archive import approve_chat, revoke_chat
from beeper_bot.retrieval import search_archive


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV = os.environ.copy()
ENV["PYTHONPATH"] = str(REPO_ROOT / "src")


class CliTest(unittest.TestCase):
    def _approval_config(self, root: Path) -> Path:
        config_path = root / "config.toml"
        config_path.write_text(f'[archive]\npath = "{root / "archive.sqlite3"}"\n')
        return config_path

    def _run_json(self, config_path: Path, *args: str) -> dict[str, object]:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["--config", str(config_path), *args, "--json"]), 0)
        return json.loads(output.getvalue())

    def _run_text(self, config_path: Path, *args: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["--config", str(config_path), *args]), 0)
        return output.getvalue().strip()

    def test_approve_list_and_revoke_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._approval_config(Path(tmpdir))
            config = load_config(config_path)
            approve_chat(config, "chat-duarte", "Duarte Mendes")
            revoke_chat(config, "chat-duarte")

            self.assertEqual(self._run_text(config_path, "approve", "chat-duarte"), "Now archiving: Duarte Mendes")
            with patch("beeper_bot.cli.make_message_client", side_effect=AssertionError("contacted Beeper")):
                listed = self._run_json(config_path, "chats", "--local")
            self.assertEqual(listed["chats"], [{"allowed": True, "chat_id": "chat-duarte", "name": "Duarte Mendes"}])

            self.assertEqual(
                self._run_text(config_path, "revoke", "chat-duarte"),
                "Stopped archiving: Duarte Mendes. Stored messages remain; run `beeper-bot forget <chat_id> --yes` to delete them.",
            )
            listed = self._run_json(config_path, "chats", "--local")
            self.assertEqual(listed["chats"][0]["allowed"], False)

    def test_forget_requires_confirmation_and_deletes_only_target_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._approval_config(Path(tmpdir))
            config = load_config(config_path)
            approve_chat(config, "chat-duarte", "Duarte Mendes")
            approve_chat(config, "chat-other", "Other Chat")
            with open_db(config.archive.path) as conn:
                for message_id, chat_id, text in (
                    ("duarte-1", "chat-duarte", "zebrastone target"),
                    ("duarte-2", "chat-duarte", "synthetic media target"),
                    ("other-1", "chat-other", "synthetic other chat"),
                ):
                    conn.execute(
                        """INSERT INTO messages(message_id, chat_id, sort_key, timestamp, message_type, text,
                           normalized_text, raw_json, created_at, updated_at) VALUES (?, ?, ?, '2026-01-01T00:00:00Z',
                           'TEXT', ?, ?, '{}', 'now', 'now')""",
                        (message_id, chat_id, 1 if message_id.endswith("1") else 2, text, text),
                    )
                    conn.execute("INSERT INTO message_fts VALUES (?, ?, ?, '', ?)", (message_id, chat_id, chat_id, text))
                conn.execute(
                    """INSERT INTO attachment_derived_text(message_id, chat_id, attachment_id, kind, derived_text,
                       created_at, updated_at) VALUES ('duarte-2', 'chat-duarte', 'media-1', 'image',
                       'synthetic media target', 'now', 'now')"""
                )
                conn.execute("INSERT INTO sync_state VALUES ('chat-duarte', 2, 'now', 'now')")
                conn.execute("INSERT INTO runtime_state VALUES ('sync_backfill_done:chat-duarte', '1', 'now')")
                conn.execute("INSERT INTO people VALUES ('person-1', 'Person One', 'now', 'now')")
                conn.execute("INSERT INTO person_chats VALUES ('person-1', 'chat-duarte')")
                conn.execute("INSERT INTO control_turns(role, content, chat_id, created_at) VALUES ('user', 'synthetic control target', 'chat-duarte', 'now')")
                conn.commit()

            self.assertEqual(len(search_archive(config, "zebrastone").results), 1)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli_main(["--config", str(config_path), "forget", "chat-duarte"]), 2)
            self.assertEqual(output.getvalue().strip(), "Refusing to delete 2 messages from Duarte Mendes without --yes.")
            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 'chat-duarte'").fetchone()[0], 2)

            self.assertEqual(self._run_text(config_path, "forget", "chat-duarte", "--yes"),
                             "Deleted 2 messages from Duarte Mendes. Nothing for this chat remains in the archive.")
            self.assertEqual(search_archive(config, "zebrastone").results, [])
            with open_db(config.archive.path) as conn:
                for table in ("messages", "message_fts", "attachment_derived_text", "sync_state", "person_chats", "control_turns"):
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE chat_id = 'chat-duarte'").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 'chat-other'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_fts WHERE chat_id = 'chat-other'").fetchone()[0], 1)
                self.assertIsNone(conn.execute("SELECT 1 FROM runtime_state WHERE key = 'sync_backfill_done:chat-duarte'").fetchone())
                chat = conn.execute("SELECT name, is_allowed FROM chats WHERE chat_id = 'chat-duarte'").fetchone()
                self.assertEqual((chat["name"], chat["is_allowed"]), ("Duarte Mendes", 0))

            self._run_text(config_path, "forget", "unknown-chat", "--yes")
            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT is_allowed FROM chats WHERE chat_id = 'unknown-chat'").fetchone()[0], 0)

    def test_forget_rolls_back_every_delete_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._approval_config(Path(tmpdir))
            config = load_config(config_path)
            approve_chat(config, "chat-duarte", "Duarte Mendes")
            with open_db(config.archive.path) as conn:
                conn.execute("""INSERT INTO messages(message_id, chat_id, sort_key, timestamp, message_type, text,
                             normalized_text, raw_json, created_at, updated_at) VALUES
                             ('duarte-1', 'chat-duarte', 1, '2026-01-01T00:00:00Z', 'TEXT', 'synthetic rollback target',
                              'synthetic rollback target', '{}', 'now', 'now')""")
                conn.execute("INSERT INTO message_fts VALUES ('duarte-1', 'chat-duarte', 'Duarte Mendes', '', 'synthetic rollback target')")
                conn.execute("CREATE TRIGGER fail_message_delete BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT, 'stop'); END")
                conn.commit()

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli_main(["--config", str(config_path), "forget", "chat-duarte", "--yes"]), 1)
            with open_db(config.archive.path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 'chat-duarte'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_fts WHERE chat_id = 'chat-duarte'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT is_allowed FROM chats WHERE chat_id = 'chat-duarte'").fetchone()[0], 1)

    def test_approve_unknown_chat_uses_id_as_local_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._approval_config(Path(tmpdir))
            approved = self._run_json(config_path, "approve", "unknown-chat")
            self.assertEqual(approved["name"], "unknown-chat")
            self.assertEqual(
                self._run_json(config_path, "chats", "--local")["chats"],
                [{"allowed": True, "chat_id": "unknown-chat", "name": "unknown-chat"}],
            )

    def test_status_json_without_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            db_path = Path(tmpdir) / "archive.sqlite3"
            config_path.write_text(
                "\n".join(
                    [
                        "[archive]",
                        f'path = "{db_path}"',
                        "",
                        "[beeper]",
                        'control_chat_id = "chat-123"',
                        'indexed_chat_ids = ["chat-a", "chat-b"]',
                    ]
                )
            )
            completed = subprocess.run(
                [sys.executable, "-m", "beeper_bot", "--config", str(config_path), "status", "--json"],
                cwd=REPO_ROOT,
                env=ENV,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["control_chat_configured"])
            self.assertEqual(payload["indexed_chat_count"], 2)
            self.assertFalse(payload["database"]["file_exists"])
            self.assertEqual(payload["database"]["schema_version"], 0)

    def test_init_db_then_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            db_path = Path(tmpdir) / "archive.sqlite3"
            config_path.write_text(
                "\n".join(
                    [
                        "[archive]",
                        f'path = "{db_path}"',
                    ]
                )
            )
            subprocess.run(
                [sys.executable, "-m", "beeper_bot", "--config", str(config_path), "init-db"],
                cwd=REPO_ROOT,
                env=ENV,
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run(
                [sys.executable, "-m", "beeper_bot", "--config", str(config_path), "status", "--json"],
                cwd=REPO_ROOT,
                env=ENV,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["database"]["file_exists"])
            self.assertEqual(payload["database"]["schema_version"], SCHEMA_VERSION)
            self.assertEqual(payload["database"]["chat_count"], 0)
            self.assertEqual(payload["database"]["message_count"], 0)

    def test_eval_json_can_force_deterministic_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            db_path = Path(tmpdir) / "archive.sqlite3"
            suite_path = Path(tmpdir) / "suite.json"
            config_path.write_text(
                "\n".join(
                    [
                        "[archive]",
                        f'path = "{db_path}"',
                        "",
                        "[beeper]",
                        'indexed_chat_ids = ["chat-a"]',
                        "",
                        "[llm]",
                        'temperature = 0.3',
                    ]
                )
            )
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
                    }
                ]
            }))
            bootstrap = "\n".join(
                [
                    "import sqlite3, sys",
                    "db = sqlite3.connect(sys.argv[1])",
                    "db.executescript('''",
                    "PRAGMA user_version = 3;",
                    "CREATE TABLE chats(chat_id TEXT PRIMARY KEY, name TEXT NOT NULL, is_allowed INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_synced_at TEXT);",
                    "CREATE TABLE messages(message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, sort_key INTEGER NOT NULL, timestamp TEXT NOT NULL, sender_id TEXT, sender_name TEXT, is_sender INTEGER NOT NULL DEFAULT 0, message_type TEXT NOT NULL, text TEXT, normalized_text TEXT, raw_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE sync_state(chat_id TEXT PRIMARY KEY, last_seen_sort_key INTEGER, last_full_sync_at TEXT, updated_at TEXT NOT NULL);",
                    "CREATE TABLE runtime_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE people(person_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE person_aliases(person_id TEXT NOT NULL, alias TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(person_id, alias));",
                    "CREATE TABLE person_chats(person_id TEXT NOT NULL, chat_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(person_id, chat_id));",
                    "CREATE TABLE control_turns(turn_id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, chat_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '', sort_key INTEGER, created_at TEXT NOT NULL);",
                    "CREATE TABLE memory_facts(fact_id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, source_kind TEXT NOT NULL DEFAULT 'memory', source_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE memory_updates(update_id INTEGER PRIMARY KEY AUTOINCREMENT, update_kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE VIRTUAL TABLE message_fts USING fts5(message_id UNINDEXED, chat_id UNINDEXED, chat_name, sender_name, text);",
                    "INSERT INTO chats VALUES('chat-a','Family logistics',1,'t','t','t');",
                    "INSERT INTO messages VALUES('msg-1','chat-a',1,'2026-05-11T14:22:00Z','u1','Seth',0,'TEXT','The address is 123 Sample St, Portland.','The address is 123 Sample St, Portland.','{}','t','t');",
                    "INSERT INTO message_fts VALUES('msg-1','chat-a','Family logistics','Seth','The address is 123 Sample St, Portland.');",
                    "''')",
                    "db.commit()",
                ]
            )
            subprocess.run(
                [sys.executable, "-c", bootstrap, str(db_path)],
                cwd=REPO_ROOT,
                env=ENV,
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "beeper_bot",
                    "--config",
                    str(config_path),
                    "eval",
                    "--suite",
                    str(suite_path),
                    "--json",
                    "--deterministic",
                ],
                cwd=REPO_ROOT,
                env=ENV,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["runtime"]["temperature"], 0.0)
            self.assertEqual(payload["runtime"]["planner_temperature"], 0.0)

    def test_find_json_returns_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            db_path = Path(tmpdir) / "archive.sqlite3"
            config_path.write_text(
                "\n".join(
                    [
                        "[archive]",
                        f'path = "{db_path}"',
                    ]
                )
            )
            bootstrap = "\n".join(
                [
                    "import sqlite3, sys",
                    "db = sqlite3.connect(sys.argv[1])",
                    "db.executescript('''",
                    "PRAGMA user_version = 3;",
                    "CREATE TABLE chats(chat_id TEXT PRIMARY KEY, name TEXT NOT NULL, is_allowed INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_synced_at TEXT);",
                    "CREATE TABLE messages(message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, sort_key INTEGER NOT NULL, timestamp TEXT NOT NULL, sender_id TEXT, sender_name TEXT, is_sender INTEGER NOT NULL DEFAULT 0, message_type TEXT NOT NULL, text TEXT, normalized_text TEXT, raw_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE sync_state(chat_id TEXT PRIMARY KEY, last_seen_sort_key INTEGER, last_full_sync_at TEXT, updated_at TEXT NOT NULL);",
                    "CREATE TABLE runtime_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE people(person_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE person_aliases(person_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY(person_id, alias));",
                    "CREATE TABLE person_chats(person_id TEXT NOT NULL, chat_id TEXT NOT NULL, PRIMARY KEY(person_id, chat_id));",
                    "CREATE TABLE control_turns(turn_id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, chat_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '', sort_key INTEGER, created_at TEXT NOT NULL);",
                    "CREATE TABLE memory_facts(fact_id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, source_kind TEXT NOT NULL DEFAULT 'memory', source_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE memory_updates(update_id INTEGER PRIMARY KEY AUTOINCREMENT, update_kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE VIRTUAL TABLE message_fts USING fts5(message_id UNINDEXED, chat_id UNINDEXED, chat_name, sender_name, text);",
                    "INSERT INTO chats VALUES('chat-a','Family logistics',1,'t','t','t');",
                    "INSERT INTO messages VALUES('msg-1','chat-a',1,'2026-05-11T14:22:00Z','u1','Seth',0,'TEXT','The address is 123 Sample St','The address is 123 Sample St','{}','t','t');",
                    "INSERT INTO message_fts VALUES('msg-1','chat-a','Family logistics','Seth','The address is 123 Sample St');",
                    "''')",
                    "db.commit()",
                ]
            )
            subprocess.run(
                [sys.executable, "-c", bootstrap, str(db_path)],
                cwd=REPO_ROOT,
                env=ENV,
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run(
                [sys.executable, "-m", "beeper_bot", "--config", str(config_path), "find", "123", "Sample", "St", "--json"],
                cwd=REPO_ROOT,
                env=ENV,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["result_count"], 1)
            self.assertEqual(payload["results"][0]["message_id"], "msg-1")


if __name__ == "__main__":
    unittest.main()
