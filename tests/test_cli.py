from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV = os.environ.copy()
ENV["PYTHONPATH"] = str(REPO_ROOT / "src")


class CliTest(unittest.TestCase):
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
            self.assertEqual(payload["database"]["schema_version"], 2)
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
                    "PRAGMA user_version = 2;",
                    "CREATE TABLE chats(chat_id TEXT PRIMARY KEY, name TEXT NOT NULL, is_allowed INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_synced_at TEXT);",
                    "CREATE TABLE messages(message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, sort_key INTEGER NOT NULL, timestamp TEXT NOT NULL, sender_id TEXT, sender_name TEXT, is_sender INTEGER NOT NULL DEFAULT 0, message_type TEXT NOT NULL, text TEXT, normalized_text TEXT, raw_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE sync_state(chat_id TEXT PRIMARY KEY, last_seen_sort_key INTEGER, last_full_sync_at TEXT, updated_at TEXT NOT NULL);",
                    "CREATE TABLE runtime_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE people(person_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE person_aliases(person_id TEXT NOT NULL, alias TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(person_id, alias));",
                    "CREATE TABLE person_chats(person_id TEXT NOT NULL, chat_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(person_id, chat_id));",
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
                    "PRAGMA user_version = 1;",
                    "CREATE TABLE chats(chat_id TEXT PRIMARY KEY, name TEXT NOT NULL, is_allowed INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_synced_at TEXT);",
                    "CREATE TABLE messages(message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, sort_key INTEGER NOT NULL, timestamp TEXT NOT NULL, sender_id TEXT, sender_name TEXT, is_sender INTEGER NOT NULL DEFAULT 0, message_type TEXT NOT NULL, text TEXT, normalized_text TEXT, raw_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
                    "CREATE TABLE sync_state(chat_id TEXT PRIMARY KEY, last_seen_sort_key INTEGER, last_full_sync_at TEXT, updated_at TEXT NOT NULL);",
                    "CREATE TABLE runtime_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);",
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
