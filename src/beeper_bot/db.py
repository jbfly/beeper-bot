from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import AppConfig, ensure_private_dir, ensure_private_file


SCHEMA_VERSION = 1


@dataclass(slots=True)
class DatabaseStats:
    path: Path
    schema_version: int
    chat_count: int
    message_count: int
    fts_count: int
    sync_state_count: int
    runtime_state_count: int
    file_exists: bool
    file_size_bytes: int


@dataclass(slots=True)
class RuntimeStatus:
    database: DatabaseStats
    config_path: Path
    control_chat_configured: bool
    indexed_chat_count: int
    beeper_api_base: str
    llm_base_url: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    ensure_private_dir(db_path.parent)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            is_allowed INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_synced_at TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            sort_key INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            sender_id TEXT,
            sender_name TEXT,
            is_sender INTEGER NOT NULL DEFAULT 0,
            message_type TEXT NOT NULL,
            text TEXT,
            normalized_text TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_sort_key
            ON messages(chat_id, sort_key);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_timestamp
            ON messages(chat_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_sender_name
            ON messages(chat_id, sender_name);

        CREATE TABLE IF NOT EXISTS sync_state (
            chat_id TEXT PRIMARY KEY,
            last_seen_sort_key INTEGER,
            last_full_sync_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
            message_id UNINDEXED,
            chat_id UNINDEXED,
            chat_name,
            sender_name,
            text
        );
        COMMIT;
        """
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def init_db_path(db_path: Path) -> None:
    ensure_private_dir(db_path.parent)
    ensure_private_file(db_path)
    with open_db(db_path) as conn:
        initialize_database(conn)


def get_schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def set_runtime_state(conn: sqlite3.Connection, key: str, value: str | dict | list) -> None:
    encoded = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO runtime_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, encoded, now),
    )
    conn.commit()


def get_runtime_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row[0])


def latest_sync_timestamp(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(updated_at) FROM sync_state").fetchone()
    if row is None or row[0] in (None, ""):
        return None
    return str(row[0])


def collect_database_stats(db_path: Path) -> DatabaseStats:
    file_exists = db_path.exists()
    file_size = db_path.stat().st_size if file_exists else 0
    if not file_exists:
        return DatabaseStats(
            path=db_path,
            schema_version=0,
            chat_count=0,
            message_count=0,
            fts_count=0,
            sync_state_count=0,
            runtime_state_count=0,
            file_exists=False,
            file_size_bytes=0,
        )

    with open_db(db_path) as conn:
        schema_version = get_schema_version(conn)
        chat_count = int(conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0])
        message_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        fts_count = int(conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0])
        sync_state_count = int(conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0])
        runtime_state_count = int(conn.execute("SELECT COUNT(*) FROM runtime_state").fetchone()[0])

    return DatabaseStats(
        path=db_path,
        schema_version=schema_version,
        chat_count=chat_count,
        message_count=message_count,
        fts_count=fts_count,
        sync_state_count=sync_state_count,
        runtime_state_count=runtime_state_count,
        file_exists=True,
        file_size_bytes=file_size,
    )


def collect_runtime_status(config: AppConfig) -> RuntimeStatus:
    return RuntimeStatus(
        database=collect_database_stats(config.archive.path),
        config_path=config.config_path,
        control_chat_configured=bool(config.beeper.control_chat_id.strip()),
        indexed_chat_count=len(config.beeper.indexed_chat_ids),
        beeper_api_base=config.beeper.api_base,
        llm_base_url=config.llm.base_url,
    )
