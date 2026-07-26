from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import AppConfig
from .db import get_runtime_state, open_db, set_runtime_state, utc_now
from .people import add_person_alias, load_person_graph, upsert_person


PENDING_UPDATE_MAX_AGE_SECONDS = 900


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


def recent_control_turns(
    config: AppConfig, limit: int = 8, chat_id: str | None = None
) -> list[dict[str, str]]:
    """Recent control-chat turns, newest last.

    When `chat_id` is given, only that control chat's turns are returned, so
    purpose-scoped chats (translation, business, …) keep separate conversational
    memory and don't bleed context into one another. Passing None (the default)
    preserves the prior all-chats behavior for the CLI ask path.
    """
    with open_db(config.archive.path) as conn:
        if chat_id:
            rows = conn.execute(
                """
                SELECT role, content
                FROM control_turns
                WHERE chat_id = ?
                ORDER BY turn_id DESC
                LIMIT ?
                """,
                (chat_id, max(1, int(limit))),
            ).fetchall()
        else:
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


CONTROL_SUMMARY_KEY = "control_summary"
CONTROL_SUMMARY_UPTO_KEY = "control_summary_upto_turn"
SUMMARY_KEEP_RECENT_TURNS = 8
SUMMARY_REFRESH_BATCH = 6
SUMMARY_MAX_WORDS = 120


def build_summary_refresh_prompt(previous_summary: str, turns: list[dict[str, str]]) -> str:
    lines = [f"- {item['role']}: {' '.join(item['content'].split())[:240]}" for item in turns]
    previous_block = previous_summary.strip() or "(none yet)"
    return (
        "Update the running summary of the user's control-chat session.\n"
        f"Keep it under {SUMMARY_MAX_WORDS} words of plain prose.\n"
        "Preserve: active goals, open threads, what was already asked and answered "
        "(so later follow-ups can be resolved), named people and what they sent or asked for, "
        "corrections, and user preferences.\n"
        "Drop greetings and chit-chat. Do not invent content.\n"
        "Do not output chain-of-thought. Return only the updated summary.\n\n"
        f"Previous summary:\n{previous_block}\n\n"
        f"New turns to fold in:\n" + "\n".join(lines)
    )


def _summarize_control_turns(config: AppConfig, prompt: str, llm_client) -> str:
    if llm_client is not None and hasattr(llm_client, "summarize_text"):
        return str(llm_client.summarize_text(config, prompt))
    from .llm import OpenAiCompatLlmClient

    client = llm_client if isinstance(llm_client, OpenAiCompatLlmClient) else OpenAiCompatLlmClient()
    return client._post_chat(
        config,
        [
            {"role": "system", "content": "Maintain running conversation summaries faithfully. No hidden reasoning."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=250,
        trace_phase="control.summary",
    )


def maybe_refresh_control_summary(config: AppConfig, llm_client=None, *, force: bool = False) -> str | None:
    """Fold control turns older than the verbatim window into the rolling
    summary. Returns the new summary when a refresh happened, else None.

    The most recent SUMMARY_KEEP_RECENT_TURNS turns stay out of the summary
    (they are sent verbatim in prompts); a refresh runs once at least
    SUMMARY_REFRESH_BATCH older turns have accumulated past the last fold.
    """
    with open_db(config.archive.path) as conn:
        row = conn.execute("SELECT MAX(turn_id) AS latest FROM control_turns").fetchone()
        latest = int(row["latest"] or 0)
        upto = int(get_runtime_state(conn, CONTROL_SUMMARY_UPTO_KEY) or 0)
        cutoff = latest - SUMMARY_KEEP_RECENT_TURNS
        if cutoff <= upto or (not force and cutoff - upto < SUMMARY_REFRESH_BATCH):
            return None
        turn_rows = conn.execute(
            """
            SELECT role, content FROM control_turns
            WHERE turn_id > ? AND turn_id <= ?
            ORDER BY turn_id ASC
            """,
            (upto, cutoff),
        ).fetchall()
        previous = get_runtime_state(conn, CONTROL_SUMMARY_KEY) or ""
    turns = [{"role": str(r["role"]), "content": str(r["content"])} for r in turn_rows if str(r["content"]).strip()]
    if not turns:
        return None

    prompt = build_summary_refresh_prompt(previous, turns)
    summary = _summarize_control_turns(config, prompt, llm_client).strip()
    if not summary:
        return None
    from .tracing import trace_event

    trace_event("control.summary.refresh", {"folded_turns": len(turns), "upto_turn": cutoff, "summary": summary})
    with open_db(config.archive.path) as conn:
        set_runtime_state(conn, CONTROL_SUMMARY_KEY, summary)
        set_runtime_state(conn, CONTROL_SUMMARY_UPTO_KEY, str(cutoff))
    return summary


def load_memory_state(config: AppConfig) -> dict:
    """Load durable memory: stored facts plus identity facts derived from the
    people graph. The people graph is the canonical store for aliases; they are
    merged here as read-only identity facts so prompt assembly and lookups see
    one uniform fact list.
    """
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
    seen_identity = {
        (item["subject"].casefold(), item["object"].casefold())
        for item in facts
        if item["predicate"] == "identity"
    }
    graph = load_person_graph(config)
    for person in graph.people:
        for alias in person.aliases:
            key = (alias.casefold(), person.canonical_name.casefold())
            if key in seen_identity:
                continue
            seen_identity.add(key)
            facts.append(
                {
                    "subject": alias,
                    "predicate": "identity",
                    "object": person.canonical_name,
                    "source": "people graph",
                }
            )
    turns = recent_control_turns(config, limit=8)
    if not summary:
        summary = _build_control_summary_from_turns(turns)
    return {"facts": facts, "control_summary": summary}


def queue_memory_update(config: AppConfig, update_kind: str, payload: dict) -> PendingMemoryUpdate:
    now = utc_now()
    with open_db(config.archive.path) as conn:
        cur = conn.execute(
            """
            INSERT INTO memory_updates(update_kind, payload_json, status, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (update_kind, json.dumps(payload, sort_keys=True), now, now),
        )
        update_id = int(cur.lastrowid)
        conn.commit()
    return PendingMemoryUpdate(update_id=update_id, update_kind=update_kind, payload=payload, created_at=now)


def queue_alias_update(config: AppConfig, alias: str, canonical_name: str, *, source_text: str = "") -> PendingMemoryUpdate:
    payload = {
        "alias": alias.strip(),
        "canonical_name": canonical_name.strip(),
        "source_text": source_text.strip(),
    }
    return queue_memory_update(config, "add-alias", payload)


def queue_proposed_action(config: AppConfig, action: dict) -> PendingMemoryUpdate | None:
    """Queue a structured proposed action attached to an ask response."""
    kind = str(action.get("kind") or "").strip()
    if kind == "add-alias":
        return queue_alias_update(
            config,
            str(action.get("alias") or ""),
            str(action.get("canonical_name") or ""),
            source_text=str(action.get("source_text") or ""),
        )
    if kind == "add-relationship-fact":
        payload = {
            "subject": str(action.get("subject") or "").strip(),
            "relationship": str(action.get("relationship") or "").strip(),
            "source_text": str(action.get("source_text") or "").strip(),
        }
        return queue_memory_update(config, "add-relationship-fact", payload)
    return None


def _pending_update_expired(created_at: str, *, max_age_seconds: int) -> bool:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > timedelta(seconds=max_age_seconds)


def latest_pending_update(
    config: AppConfig,
    *,
    max_age_seconds: int = PENDING_UPDATE_MAX_AGE_SECONDS,
) -> PendingMemoryUpdate | None:
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
    if _pending_update_expired(str(row["created_at"]), max_age_seconds=max_age_seconds):
        clear_pending_update(config, int(row["update_id"]), status="expired")
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
    if update.update_kind == "add-alias":
        alias = str(update.payload.get("alias") or "").strip()
        canonical_name = str(update.payload.get("canonical_name") or "").strip()
        if not alias or not canonical_name:
            raise ValueError("Pending alias update is incomplete")
        graph = load_person_graph(config)
        person = graph.find_person(canonical_name)
        person_id = person.person_id if person else _slugify(canonical_name)
        upsert_person(config, person_id, canonical_name)
        add_person_alias(config, person_id, alias)
        clear_pending_update(config, update.update_id, status="applied")
        return f"Saved alias: {alias} → {canonical_name}."

    if update.update_kind == "add-relationship-fact":
        subject = str(update.payload.get("subject") or "").strip()
        relationship = str(update.payload.get("relationship") or "").strip()
        if not subject or not relationship:
            raise ValueError("Pending relationship update is incomplete")
        add_memory_fact(
            config,
            subject,
            "relationship_to_user",
            relationship,
            source_kind="user-approved fact",
            source_text=str(update.payload.get("source_text") or "").strip(),
        )
        clear_pending_update(config, update.update_id, status="applied")
        return f"Saved: {subject} is your {relationship}."

    raise ValueError(f"Unsupported pending update kind: {update.update_kind}")


def _normalize_reply(text: str) -> str:
    cleaned = re.sub(r"[^a-z' ]+", " ", text.strip().casefold())
    return re.sub(r"\s+", " ", cleaned).strip()


CONFIRMATION_PHRASES = {
    "yes",
    "yes save it",
    "yes please",
    "save it",
    "save that",
    "save",
    "confirm",
    "confirmed",
    "yes confirm",
    "do it",
    "yes do it",
    "go ahead",
    "yep",
    "yeah",
}

REJECTION_PHRASES = {
    "no",
    "no thanks",
    "nope",
    "cancel",
    "never mind",
    "nevermind",
    "dont save it",
    "don't save it",
    "dont save that",
    "don't save that",
    "forget it",
}


def looks_like_confirmation(text: str) -> bool:
    return _normalize_reply(text) in CONFIRMATION_PHRASES


def looks_like_rejection(text: str) -> bool:
    return _normalize_reply(text) in REJECTION_PHRASES
