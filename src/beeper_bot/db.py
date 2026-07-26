from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import AppConfig, ensure_private_dir, ensure_private_file


SCHEMA_VERSION = 9


@dataclass(slots=True)
class DatabaseStats:
    path: Path
    schema_version: int
    chat_count: int
    message_count: int
    fts_count: int
    sync_state_count: int
    runtime_state_count: int
    people_count: int
    control_turn_count: int
    memory_fact_count: int
    pending_update_count: int
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
            is_allowed INTEGER NOT NULL DEFAULT 0,
            approval_source TEXT NOT NULL DEFAULT '',
            approved_at TEXT,
            revoked_at TEXT,
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
            source_kind TEXT NOT NULL DEFAULT 'beeper',
            source_ref TEXT NOT NULL DEFAULT '',
            source_artifact_sha256 TEXT NOT NULL DEFAULT '',
            evidence_fingerprint TEXT NOT NULL DEFAULT '',
            possible_duplicate_of TEXT,
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
        CREATE INDEX IF NOT EXISTS idx_messages_chat_evidence_fingerprint
            ON messages(chat_id, evidence_fingerprint);

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
            text,
            tokenize = 'porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS people (
            person_id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS person_aliases (
            person_id TEXT NOT NULL,
            alias TEXT NOT NULL,
            PRIMARY KEY (person_id, alias),
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );

        CREATE TABLE IF NOT EXISTS person_chats (
            person_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            PRIMARY KEY (person_id, chat_id),
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );

        CREATE TABLE IF NOT EXISTS control_turns (
            turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            chat_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            sort_key INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_facts (
            fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            source_kind TEXT NOT NULL DEFAULT 'memory',
            source_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_updates (
            update_id INTEGER PRIMARY KEY AUTOINCREMENT,
            update_kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            trace_kind TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'console',
            question TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            final_answer TEXT NOT NULL DEFAULT '',
            error_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS trace_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            seq_no INTEGER NOT NULL,
            event_kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(trace_id) REFERENCES traces(trace_id)
        );
        CREATE INDEX IF NOT EXISTS idx_trace_events_trace_seq ON trace_events(trace_id, seq_no, event_id);

        CREATE TABLE IF NOT EXISTS telemetry_samples (
            sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            gpu_util REAL,
            vram_used_mb REAL,
            vram_total_mb REAL,
            gpu_temp_c REAL,
            error_text TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS attachment_derived_text (
            derived_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            attachment_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            derived_text TEXT NOT NULL DEFAULT '',
            model_alias TEXT NOT NULL DEFAULT '',
            chunk_count INTEGER NOT NULL DEFAULT 0,
            duration_seconds REAL,
            error_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(message_id, attachment_id)
        );

        CREATE TABLE IF NOT EXISTS outbound_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
        COMMIT;
        """
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 2:
        return
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE IF NOT EXISTS people (
            person_id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS person_aliases (
            person_id TEXT NOT NULL,
            alias TEXT NOT NULL,
            PRIMARY KEY (person_id, alias),
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );
        CREATE TABLE IF NOT EXISTS person_chats (
            person_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            PRIMARY KEY (person_id, chat_id),
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        );
        COMMIT;
        """
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 3:
        return
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE IF NOT EXISTS control_turns (
            turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            chat_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            sort_key INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_facts (
            fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            source_kind TEXT NOT NULL DEFAULT 'memory',
            source_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_updates (
            update_id INTEGER PRIMARY KEY AUTOINCREMENT,
            update_kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        COMMIT;
        """
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()


def migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 4:
        return
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            trace_kind TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'console',
            question TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            final_answer TEXT NOT NULL DEFAULT '',
            error_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS trace_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            seq_no INTEGER NOT NULL,
            event_kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(trace_id) REFERENCES traces(trace_id)
        );
        CREATE INDEX IF NOT EXISTS idx_trace_events_trace_seq ON trace_events(trace_id, seq_no, event_id);
        CREATE TABLE IF NOT EXISTS telemetry_samples (
            sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            gpu_util REAL,
            vram_used_mb REAL,
            vram_total_mb REAL,
            gpu_temp_c REAL,
            error_text TEXT NOT NULL DEFAULT ''
        );
        COMMIT;
        """
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()


def migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Rebuild message_fts with porter stemming so morphological variants
    match (owe/owes/owed). Porter only stems English suffixes; names,
    addresses, and numbers are unaffected."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 5:
        return
    conn.executescript(
        """
        BEGIN;
        DROP TABLE IF EXISTS message_fts;
        CREATE VIRTUAL TABLE message_fts USING fts5(
            message_id UNINDEXED,
            chat_id UNINDEXED,
            chat_name,
            sender_name,
            text,
            tokenize = 'porter unicode61'
        );
        INSERT INTO message_fts(message_id, chat_id, chat_name, sender_name, text)
        SELECT m.message_id, m.chat_id, COALESCE(c.name, ''), COALESCE(m.sender_name, ''), m.text
        FROM messages m
        LEFT JOIN chats c ON c.chat_id = m.chat_id
        WHERE m.text IS NOT NULL AND m.text != '';
        COMMIT;
        """
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()


def migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 6:
        return
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE IF NOT EXISTS attachment_derived_text (
            derived_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            attachment_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            derived_text TEXT NOT NULL DEFAULT '',
            model_alias TEXT NOT NULL DEFAULT '',
            chunk_count INTEGER NOT NULL DEFAULT 0,
            duration_seconds REAL,
            error_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(message_id, attachment_id)
        );
        COMMIT;
        """
    )
    conn.execute("PRAGMA user_version = 6")
    conn.commit()


def migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 7:
        return
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE IF NOT EXISTS outbound_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
        COMMIT;
        """
    )
    conn.execute("PRAGMA user_version = 7")
    conn.commit()


def migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 8:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE chats_v8 (
            chat_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            is_allowed INTEGER NOT NULL DEFAULT 0,
            approval_source TEXT NOT NULL DEFAULT '',
            approved_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_synced_at TEXT
        );
        INSERT INTO chats_v8(chat_id, name, is_allowed, approval_source, approved_at, created_at, updated_at, last_synced_at)
        SELECT chat_id, name, is_allowed,
               CASE WHEN is_allowed = 1 THEN 'legacy-migration' ELSE '' END,
               CASE WHEN is_allowed = 1 THEN updated_at ELSE NULL END,
               created_at, updated_at, last_synced_at
        FROM chats;
        DROP TABLE chats;
        ALTER TABLE chats_v8 RENAME TO chats;
        ALTER TABLE messages ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'beeper';
        ALTER TABLE messages ADD COLUMN source_ref TEXT NOT NULL DEFAULT '';
        COMMIT;
        """
    )
    conn.execute("PRAGMA user_version = 8")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 9:
        return
    conn.executescript(
        """
        BEGIN;
        ALTER TABLE messages ADD COLUMN source_artifact_sha256 TEXT NOT NULL DEFAULT '';
        ALTER TABLE messages ADD COLUMN evidence_fingerprint TEXT NOT NULL DEFAULT '';
        ALTER TABLE messages ADD COLUMN possible_duplicate_of TEXT;
        CREATE INDEX IF NOT EXISTS idx_messages_chat_evidence_fingerprint
            ON messages(chat_id, evidence_fingerprint);
        COMMIT;
        """
    )
    conn.execute("PRAGMA user_version = 9")
    conn.commit()


def init_db_path(db_path: Path) -> None:
    ensure_private_dir(db_path.parent)
    ensure_private_file(db_path)
    with open_db(db_path) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            initialize_database(conn)
            return
        if version == 1:
            migrate_v1_to_v2(conn)
        if version <= 2:
            migrate_v2_to_v3(conn)
        if version <= 3:
            migrate_v3_to_v4(conn)
        if version <= 4:
            migrate_v4_to_v5(conn)
        if version <= 5:
            migrate_v5_to_v6(conn)
        if version <= 6:
            migrate_v6_to_v7(conn)
        if version <= 7:
            migrate_v7_to_v8(conn)
        if version <= 8:
            migrate_v8_to_v9(conn)


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


def enqueue_outbound(conn: sqlite3.Connection, target: str, text: str) -> int:
    """Queue a message for the serve loop to deliver to a control chat.

    This is the primitive behind `beeper-bot notify`: any homelab script can
    append a fire-and-forget message (a download finished, a backup failed, an
    alert) without holding a live Matrix client. The running serve loop drains
    the queue and sends via its already-open transport, so delivery survives the
    bot being momentarily down.
    """
    now = utc_now()
    cur = conn.execute(
        "INSERT INTO outbound_queue(target, text, created_at) VALUES (?, ?, ?)",
        (target.strip() or "main", text, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_unsent_outbound(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT id, target, text
        FROM outbound_queue
        WHERE sent_at IS NULL
        ORDER BY id ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [{"id": int(row["id"]), "target": str(row["target"]), "text": str(row["text"])} for row in rows]


def mark_outbound_sent(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute(
        "UPDATE outbound_queue SET sent_at = ? WHERE id = ?",
        (utc_now(), int(row_id)),
    )
    conn.commit()


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
            people_count=0,
            control_turn_count=0,
            memory_fact_count=0,
            pending_update_count=0,
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
        people_count = int(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0])
        control_turn_count = int(conn.execute("SELECT COUNT(*) FROM control_turns").fetchone()[0])
        memory_fact_count = int(conn.execute("SELECT COUNT(*) FROM memory_facts WHERE status = 'active'").fetchone()[0])
        pending_update_count = int(conn.execute("SELECT COUNT(*) FROM memory_updates WHERE status = 'pending'").fetchone()[0])

    return DatabaseStats(
        path=db_path,
        schema_version=schema_version,
        chat_count=chat_count,
        message_count=message_count,
        fts_count=fts_count,
        sync_state_count=sync_state_count,
        runtime_state_count=runtime_state_count,
        people_count=people_count,
        control_turn_count=control_turn_count,
        memory_fact_count=memory_fact_count,
        pending_update_count=pending_update_count,
        file_exists=True,
        file_size_bytes=file_size,
    )


def collect_runtime_status(config: AppConfig) -> RuntimeStatus:
    return RuntimeStatus(
        database=collect_database_stats(config.archive.path),
        config_path=config.config_path,
        control_chat_configured=bool(config.beeper.control_chat_id.strip() or config.control_chats),
        indexed_chat_count=len(config.beeper.indexed_chat_ids),
        beeper_api_base=config.beeper.api_base,
        llm_base_url=config.llm.base_url,
    )
