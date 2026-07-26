from __future__ import annotations

import calendar
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .config import AppConfig, ConfigError
from .db import init_db_path, open_db, utc_now
from .retrieval import search_archive
from .sync import BACKFILL_DONE_KEY_PREFIX, find_possible_duplicate, normalize_text, normalized_evidence_fingerprint

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_ZIP_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ZIP_MEMBERS = 256
MESSAGE_RE = re.compile(
    r"^(?:\[)?(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),?\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)(?:\])?\s*(?:-|–)\s*(?P<body>.*)$",
    re.IGNORECASE,
)
BRACKET_RE = re.compile(
    r"^\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)\]\s*(?P<body>.*)$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ImportedMessage:
    timestamp: str
    sender_name: str
    text: str
    line_number: int


def approve_chat(config: AppConfig, chat_id: str, name: str, source: str = "operator") -> dict[str, object]:
    chat_id = chat_id.strip()
    if not chat_id:
        raise ConfigError("chat_id must not be empty")
    init_db_path(config.archive.path)
    now = utc_now()
    with open_db(config.archive.path) as conn:
        conn.execute(
            """
            INSERT INTO chats(chat_id, name, is_allowed, approval_source, approved_at, revoked_at, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                name = excluded.name, is_allowed = 1, approval_source = excluded.approval_source,
                approved_at = excluded.approved_at, revoked_at = NULL, updated_at = excluded.updated_at
            """,
            (chat_id, name.strip() or chat_id, source, now, now, now),
        )
        conn.commit()
    return {"chat_id": chat_id, "name": name.strip() or chat_id, "approved": True,
            "approval_source": source, "approved_at": now}


def revoke_chat(config: AppConfig, chat_id: str) -> bool:
    init_db_path(config.archive.path)
    now = utc_now()
    with open_db(config.archive.path) as conn:
        cursor = conn.execute(
            "UPDATE chats SET is_allowed = 0, revoked_at = ?, updated_at = ? WHERE chat_id = ?",
            (now, now, chat_id.strip()),
        )
        conn.commit()
        return cursor.rowcount > 0


def forget_chat(config: AppConfig, chat_id: str, *, confirmed: bool = False) -> dict[str, object]:
    chat_id = chat_id.strip()
    if not chat_id:
        raise ConfigError("chat_id must not be empty")
    init_db_path(config.archive.path)
    now = utc_now()
    with open_db(config.archive.path) as conn:
        if confirmed:
            conn.execute("BEGIN IMMEDIATE")
        try:
            chat = conn.execute("SELECT name FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
            name = str(chat["name"]) if chat else chat_id
            message_count = int(conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0])
            quoted_fact_count = int(conn.execute("SELECT COUNT(*) FROM memory_facts WHERE source_text != ''").fetchone()[0])
            if not confirmed:
                return {"chat_id": chat_id, "name": name, "message_count": message_count,
                        "quoted_fact_count": quoted_fact_count, "deleted": False}

            conn.execute(
                """
                INSERT INTO chats(chat_id, name, is_allowed, approval_source, revoked_at, created_at, updated_at)
                VALUES (?, ?, 0, '', ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    is_allowed = 0, revoked_at = excluded.revoked_at,
                    updated_at = excluded.updated_at, last_synced_at = NULL
                """,
                (chat_id, name, now, now, now),
            )
            conn.execute("DELETE FROM trace_events")
            conn.execute("DELETE FROM traces")
            conn.execute("DELETE FROM memory_updates")
            for table in ("attachment_derived_text", "message_fts", "control_turns", "messages", "sync_state", "person_chats"):
                conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM runtime_state WHERE key = ?", (f"{BACKFILL_DONE_KEY_PREFIX}{chat_id}",))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"chat_id": chat_id, "name": name, "message_count": message_count,
            "quoted_fact_count": quoted_fact_count, "deleted": True}


def list_chats(config: AppConfig) -> list[dict[str, object]]:
    init_db_path(config.archive.path)
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            "SELECT chat_id, name, is_allowed FROM chats ORDER BY name, chat_id"
        ).fetchall()
    return [{"chat_id": row["chat_id"], "name": row["name"], "allowed": bool(row["is_allowed"])} for row in rows]


def list_approved_chats(config: AppConfig) -> list[dict[str, object]]:
    init_db_path(config.archive.path)
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            """
            SELECT c.chat_id, c.name, c.approval_source, c.approved_at, COUNT(m.message_id) AS message_count
            FROM chats c LEFT JOIN messages m ON m.chat_id = c.chat_id
            WHERE c.is_allowed = 1
            GROUP BY c.chat_id ORDER BY c.name, c.chat_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _read_export(path: Path) -> tuple[str, str, str]:
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise ConfigError(f"import path is not a regular file: {path}")
    size = path.stat().st_size
    if size > MAX_ZIP_BYTES:
        raise ConfigError(f"import file is too large: {size} bytes")
    artifact_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if path.suffix.casefold() == ".txt":
        if size > MAX_INPUT_BYTES:
            raise ConfigError(f"text export is too large: {size} bytes")
        data, source_name = path.read_bytes(), path.name
    elif path.suffix.casefold() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if not members or len(members) > MAX_ZIP_MEMBERS:
                    raise ConfigError("ZIP has an unreasonable number of members")
                total = 0
                text_members: list[zipfile.ZipInfo] = []
                for member in members:
                    member_path = PurePosixPath(member.filename)
                    if member.is_dir():
                        continue
                    if member.flag_bits & 0x1:
                        raise ConfigError("encrypted ZIP members are not supported")
                    if "\\" in member.filename or member_path.is_absolute() or ".." in member_path.parts or member.filename.startswith(("/", "\\")):
                        raise ConfigError("ZIP member path traversal is not allowed")
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ConfigError("ZIP symlinks are not allowed")
                    total += member.file_size
                    if total > MAX_ZIP_EXPANDED_BYTES:
                        raise ConfigError("ZIP expands beyond the size limit")
                    if member.file_size > MAX_INPUT_BYTES and member_path.suffix.casefold() == ".txt":
                        raise ConfigError("chat text member is too large")
                    if member.compress_size and member.file_size / member.compress_size > 100:
                        raise ConfigError("ZIP member compression ratio is unreasonable")
                    if member_path.suffix.casefold() == ".txt":
                        text_members.append(member)
                if len(text_members) != 1:
                    raise ConfigError("ZIP must contain exactly one chat .txt file")
                member = text_members[0]
                data, source_name = archive.read(member), PurePosixPath(member.filename).name
        except zipfile.BadZipFile as exc:
            raise ConfigError("invalid ZIP export") from exc
    else:
        raise ConfigError("import path must end in .txt or .zip")
    try:
        return data.decode("utf-8-sig"), source_name, artifact_sha256
    except UnicodeDecodeError as exc:
        raise ConfigError("chat export must be UTF-8 text") from exc


def _parse_timestamp(date_text: str, time_text: str, day_first: bool) -> str:
    value = f"{date_text} {time_text.strip().upper()}"
    date_formats = ("%d/%m/%y", "%d/%m/%Y") if day_first else ("%m/%d/%y", "%m/%d/%Y")
    time_formats = ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S")
    for date_format in date_formats:
        for time_format in time_formats:
            try:
                return datetime.strptime(value, f"{date_format} {time_format}").isoformat(timespec="seconds")
            except ValueError:
                pass
    raise ConfigError(f"timestamp does not match the selected date order: {date_text} {time_text}")


def parse_whatsapp_export(text: str, date_order: str = "auto") -> list[ImportedMessage]:
    if date_order not in {"auto", "day-first", "month-first"}:
        raise ConfigError("date order must be auto, day-first, or month-first")
    matches = [BRACKET_RE.match(line) or MESSAGE_RE.match(line) for line in text.splitlines()]
    if date_order == "auto":
        inferred: set[str] = set()
        for match in matches:
            if not match:
                continue
            first, second = map(int, match.group("date").split("/")[:2])
            if first > 12:
                inferred.add("day-first")
            elif second > 12:
                inferred.add("month-first")
        if len(inferred) > 1:
            raise ConfigError("WhatsApp export contains conflicting date orders; specify --date-order")
        if not inferred:
            raise ConfigError("WhatsApp dates are ambiguous; specify --date-order day-first or month-first")
        date_order = inferred.pop()
    day_first = date_order == "day-first"
    messages: list[ImportedMessage] = []
    current: ImportedMessage | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = BRACKET_RE.match(line) or MESSAGE_RE.match(line)
        if match:
            body = match.group("body")
            sender, separator, message_text = body.partition(": ")
            if not separator or not sender.strip():
                current = None
                continue
            current = ImportedMessage(_parse_timestamp(match.group("date"), match.group("time"), day_first),
                                      sender.strip(), message_text, line_number)
            messages.append(current)
        elif current is not None:
            current.text += "\n" + line
    return [message for message in messages if message.text.strip()]

def import_whatsapp(config: AppConfig, path: Path, chat_id: str, name: str | None = None, date_order: str = "auto") -> dict[str, object]:
    init_db_path(config.archive.path)
    chat_id = chat_id.strip()
    with open_db(config.archive.path) as conn:
        chat = conn.execute("SELECT name, is_allowed FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
    if chat is None or int(chat["is_allowed"]) != 1:
        raise ConfigError("chat is not approved; approve this stable chat_id before importing")

    text, source_name, artifact_sha256 = _read_export(path)
    parsed = parse_whatsapp_export(text, date_order=date_order)
    if not parsed:
        raise ConfigError("no WhatsApp messages found in export")
    display_name = (name or str(chat["name"])).strip() or chat_id
    now = utc_now()
    occurrences: dict[str, int] = {}
    with open_db(config.archive.path) as conn:
        conn.execute("BEGIN")
        allowed = conn.execute("UPDATE chats SET name = ?, updated_at = ? WHERE chat_id = ? AND is_allowed = 1", (display_name, now, chat_id))
        if allowed.rowcount != 1:
            conn.rollback()
            raise ConfigError("chat approval was revoked before import")
        for position, message in enumerate(parsed):
            identity = "\0".join((chat_id, message.timestamp, message.sender_name, message.text))
            occurrences[identity] = occurrences.get(identity, 0) + 1
            message_id = "wa:" + hashlib.sha256(f"{identity}\0{occurrences[identity]}".encode()).hexdigest()
            sort_key = calendar.timegm(datetime.fromisoformat(message.timestamp).timetuple()) * 1_000_000 + position
            existing = conn.execute("SELECT sort_key FROM messages WHERE message_id = ?", (message_id,)).fetchone()
            if existing is not None:
                sort_key = int(existing["sort_key"])
            else:
                while conn.execute("SELECT 1 FROM messages WHERE chat_id = ? AND sort_key = ?", (chat_id, sort_key)).fetchone():
                    sort_key += 1
            source_ref = f"{source_name}#L{message.line_number}"
            fingerprint = normalized_evidence_fingerprint(message.text, message.sender_name, message.timestamp)
            possible_duplicate = find_possible_duplicate(conn, chat_id, message_id, "whatsapp-export", fingerprint)
            conn.execute(
                """
                INSERT INTO messages(message_id, chat_id, sort_key, timestamp, sender_id, sender_name, is_sender,
                    message_type, text, normalized_text, raw_json, source_kind, source_ref,
                    source_artifact_sha256, evidence_fingerprint, possible_duplicate_of, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', ?, 0, 'TEXT', ?, ?, ?, 'whatsapp-export', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    chat_id = excluded.chat_id, sort_key = excluded.sort_key, timestamp = excluded.timestamp,
                    sender_name = excluded.sender_name, text = excluded.text, normalized_text = excluded.normalized_text,
                    source_kind = excluded.source_kind, source_ref = excluded.source_ref,
                    source_artifact_sha256 = excluded.source_artifact_sha256,
                    evidence_fingerprint = excluded.evidence_fingerprint,
                    possible_duplicate_of = COALESCE(excluded.possible_duplicate_of, messages.possible_duplicate_of),
                    updated_at = excluded.updated_at
                """,
                (message_id, chat_id, sort_key, message.timestamp, message.sender_name, message.text,
                 normalize_text(message.text), json.dumps({"source_kind": "whatsapp-export", "source_ref": source_ref,
                                                           "source_artifact_sha256": artifact_sha256}),
                 source_ref, artifact_sha256, fingerprint, possible_duplicate, now, now),
            )
            conn.execute("DELETE FROM message_fts WHERE message_id = ?", (message_id,))
            conn.execute("INSERT INTO message_fts(message_id, chat_id, chat_name, sender_name, text) VALUES (?, ?, ?, ?, ?)",
                         (message_id, chat_id, display_name, message.sender_name, message.text))
        conn.commit()
    return {"chat_id": chat_id, "chat_name": display_name, "source": source_name,
            "source_artifact_sha256": artifact_sha256, "message_count": len(parsed)}


def scoped_search(config: AppConfig, chat_id: str, query: str, limit: int = 20) -> list[dict[str, object]]:
    response = search_archive(config, query, limit=limit, chat_ids=[chat_id])
    with open_db(config.archive.path) as conn:
        sources = {str(row["message_id"]): {"kind": str(row["source_kind"]), "ref": str(row["source_ref"] or row["message_id"]),
                                                    "artifact_sha256": str(row["source_artifact_sha256"]),
                                                    "evidence_fingerprint": str(row["evidence_fingerprint"]),
                                                    "possible_duplicate_of": row["possible_duplicate_of"]}
                   for row in conn.execute("""SELECT m.message_id, m.source_kind, m.source_ref, m.source_artifact_sha256,
                                                        m.evidence_fingerprint, m.possible_duplicate_of
                                                 FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                                                 WHERE m.chat_id = ? AND c.is_allowed = 1""", (chat_id,))}
    return [{"citation": {"message_id": item.message_id, "source": sources[item.message_id]},
             "chat_id": item.chat_id, "chat_name": item.chat_name, "sender": item.sender_name,
             "timestamp": item.timestamp, "text": item.text, "score": item.score}
            for item in response.results if item.message_id in sources]


def surrounding_thread(config: AppConfig, chat_id: str, message_id: str, radius: int = 3) -> list[dict[str, object]]:
    radius = max(0, min(int(radius), 50))
    with open_db(config.archive.path) as conn:
        anchor = conn.execute(
            """SELECT m.sort_key FROM messages m JOIN chats c ON c.chat_id = m.chat_id
               WHERE m.message_id = ? AND m.chat_id = ? AND c.is_allowed = 1""", (message_id, chat_id)).fetchone()
        if anchor is None:
            return []
        columns = "m.message_id, m.sender_name, m.timestamp, m.text, m.source_kind, m.source_ref, m.source_artifact_sha256, m.evidence_fingerprint, m.possible_duplicate_of, m.sort_key"
        before = conn.execute(
            f"""SELECT {columns} FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                 WHERE m.chat_id = ? AND c.is_allowed = 1 AND m.sort_key < ?
                 ORDER BY m.sort_key DESC LIMIT ?""", (chat_id, int(anchor["sort_key"]), radius)).fetchall()
        center = conn.execute(
            f"""SELECT {columns} FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                 WHERE m.message_id = ? AND m.chat_id = ? AND c.is_allowed = 1""", (message_id, chat_id)).fetchall()
        after = conn.execute(
            f"""SELECT {columns} FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                 WHERE m.chat_id = ? AND c.is_allowed = 1 AND m.sort_key > ?
                 ORDER BY m.sort_key ASC LIMIT ?""", (chat_id, int(anchor["sort_key"]), radius)).fetchall()
    rows = [*reversed(before), *center, *after]
    return [{"citation": {"message_id": row["message_id"],
                           "source": {"kind": row["source_kind"], "ref": row["source_ref"] or row["message_id"],
                                      "artifact_sha256": row["source_artifact_sha256"],
                                      "evidence_fingerprint": row["evidence_fingerprint"],
                                      "possible_duplicate_of": row["possible_duplicate_of"]}},
             "sender": row["sender_name"] or "", "timestamp": row["timestamp"], "text": row["text"] or ""}
            for row in rows]
