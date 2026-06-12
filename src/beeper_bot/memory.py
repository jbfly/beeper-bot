from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import AppConfig
from .db import get_runtime_state, open_db, utc_now
from .people import add_person_alias, load_person_graph, upsert_person


@dataclass(slots=True)
class PendingMemoryUpdate:
    update_id: int
    update_kind: str
    payload: dict
    created_at: str


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().casefold()).strip("-")
    return cleaned or "person"


def record_control_turn(
    config: AppConfig,
    role: str,
    content: str,
    *,
    chat_id: str = "",
    message_id: str = "",
    sort_key: int | None = None,
) -> None:
    text = content.strip()
    if not text:
        return
    now = utc_now()
    with open_db(config.archive.path) as conn:
        conn.execute(
            """
            INSERT INTO control_turns(role, content, chat_id, message_id, sort_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (role.strip() or "unknown", text, chat_id.strip(), message_id.strip(), sort_key, now),
        )
        conn.commit()


def recent_control_turns(config: AppConfig, limit: int = 8) -> list[dict[str, str]]:
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            """
            SELECT role, content
            FROM control_turns
            ORDER BY turn_id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [{"role": str(row["role"]), "content": str(row["content"])} for row in reversed(rows)]


def add_memory_fact(
    config: AppConfig,
    subject: str,
    predicate: str,
    obj: str,
    *,
    source_kind: str = "user-approved fact",
    source_text: str = "",
) -> None:
    now = utc_now()
    with open_db(config.archive.path) as conn:
        conn.execute(
            """
            INSERT INTO memory_facts(subject, predicate, object, source_kind, source_text, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (subject.strip(), predicate.strip(), obj.strip(), source_kind.strip(), source_text.strip(), now, now),
        )
        conn.commit()


def _build_control_summary_from_turns(turns: list[dict[str, str]]) -> str:
    user_turns = [item["content"].strip() for item in turns if item.get("role") == "user" and item.get("content")]
    if not user_turns:
        return ""
    topics: list[str] = []
    seen: set[str] = set()
    for content in reversed(user_turns):
        clean = re.sub(r"\s+", " ", content).strip()
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        topics.append(clean)
        if len(topics) >= 4:
            break
    topics.reverse()
    return "Recent control topics: " + " | ".join(topics)


def load_memory_state(config: AppConfig) -> dict:
    with open_db(config.archive.path) as conn:
        fact_rows = conn.execute(
            """
            SELECT subject, predicate, object, source_kind, source_text
            FROM memory_facts
            WHERE status = 'active'
            ORDER BY fact_id ASC
            """
        ).fetchall()
        summary = get_runtime_state(conn, "control_summary") or ""
    facts = [
        {
            "subject": str(row["subject"]),
            "predicate": str(row["predicate"]),
            "object": str(row["object"]),
            "source": str(row["source_kind"] or row["source_text"] or "memory"),
        }
        for row in fact_rows
    ]
    turns = recent_control_turns(config, limit=8)
    if not summary:
        summary = _build_control_summary_from_turns(turns)
    return {"facts": facts, "control_summary": summary}


def queue_alias_update(config: AppConfig, alias: str, canonical_name: str, *, source_text: str = "") -> PendingMemoryUpdate:
    payload = {
        "alias": alias.strip(),
        "canonical_name": canonical_name.strip(),
        "source_text": source_text.strip(),
    }
    now = utc_now()
    with open_db(config.archive.path) as conn:
        cur = conn.execute(
            """
            INSERT INTO memory_updates(update_kind, payload_json, status, created_at, updated_at)
            VALUES ('add-alias', ?, 'pending', ?, ?)
            """,
            (json.dumps(payload, sort_keys=True), now, now),
        )
        update_id = int(cur.lastrowid)
        conn.commit()
    return PendingMemoryUpdate(update_id=update_id, update_kind="add-alias", payload=payload, created_at=now)


def latest_pending_update(config: AppConfig) -> PendingMemoryUpdate | None:
    with open_db(config.archive.path) as conn:
        row = conn.execute(
            """
            SELECT update_id, update_kind, payload_json, created_at
            FROM memory_updates
            WHERE status = 'pending'
            ORDER BY update_id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload_json"]))
    return PendingMemoryUpdate(
        update_id=int(row["update_id"]),
        update_kind=str(row["update_kind"]),
        payload=payload,
        created_at=str(row["created_at"]),
    )


def clear_pending_update(config: AppConfig, update_id: int, *, status: str = "cancelled") -> None:
    now = utc_now()
    with open_db(config.archive.path) as conn:
        conn.execute(
            "UPDATE memory_updates SET status = ?, updated_at = ? WHERE update_id = ?",
            (status, now, update_id),
        )
        conn.commit()


def apply_pending_update(config: AppConfig, update: PendingMemoryUpdate) -> str:
    if update.update_kind != "add-alias":
        raise ValueError(f"Unsupported pending update kind: {update.update_kind}")

    alias = str(update.payload.get("alias") or "").strip()
    canonical_name = str(update.payload.get("canonical_name") or "").strip()
    if not alias or not canonical_name:
        raise ValueError("Pending alias update is incomplete")

    graph = load_person_graph(config)
    person = graph.find_person(canonical_name)
    person_id = person.person_id if person else _slugify(canonical_name)
    upsert_person(config, person_id, canonical_name)
    add_person_alias(config, person_id, alias)
    add_memory_fact(
        config,
        alias,
        "identity",
        canonical_name,
        source_kind="user-approved fact",
        source_text=str(update.payload.get("source_text") or "").strip(),
    )
    clear_pending_update(config, update.update_id, status="applied")
    return f"Saved alias: {alias} → {canonical_name}."


def looks_like_confirmation(text: str) -> bool:
    lowered = text.strip().casefold()
    return lowered in {
        "yes",
        "yes save it",
        "save it",
        "confirm",
        "yes confirm",
        "do it",
        "yes do it",
        "save that",
    }


def looks_like_rejection(text: str) -> bool:
    lowered = text.strip().casefold()
    return lowered in {"no", "no thanks", "cancel", "never mind", "dont save it", "don't save it"}
