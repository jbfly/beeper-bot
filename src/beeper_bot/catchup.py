from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .db import get_runtime_state, open_db, set_runtime_state, utc_now
from .tracing import trace_event


CATCHUP_CURSOR_PREFIX = "catchup_cursor:"
MAX_CATCHUP_MESSAGES = 300
MAX_LINE_CHARS = 280


class CatchupError(RuntimeError):
    pass


@dataclass(slots=True)
class CatchupResult:
    chat_id: str
    chat_name: str
    since_sort_key: int | None
    latest_sort_key: int | None
    message_count: int
    truncated: bool
    summary: str


def resolve_chat(config: AppConfig, chat_query: str) -> tuple[str, str]:
    """Resolve a chat title query against the archive; newest activity wins ties."""
    query = chat_query.strip().casefold()
    if not query:
        raise CatchupError("Chat name is required")
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            """
            SELECT c.chat_id, c.name, MAX(m.sort_key) AS latest
            FROM chats c
            LEFT JOIN messages m ON m.chat_id = c.chat_id
            GROUP BY c.chat_id, c.name
            """
        ).fetchall()
    scored: list[tuple[int, int, str, str]] = []
    for row in rows:
        name = str(row["name"] or "")
        lowered = name.casefold()
        if query == lowered:
            score = 3
        elif query in lowered:
            score = 2
        elif all(token in lowered for token in query.split() if len(token) >= 3):
            score = 1
        else:
            continue
        scored.append((score, int(row["latest"] or 0), str(row["chat_id"]), name))
    if not scored:
        raise CatchupError(f"No indexed chat matches '{chat_query.strip()}'")
    scored.sort(reverse=True)
    _, _, chat_id, name = scored[0]
    return chat_id, name


def _cursor_key(chat_id: str) -> str:
    return f"{CATCHUP_CURSOR_PREFIX}{chat_id}"


def _messages_since(config: AppConfig, chat_id: str, since_sort_key: int | None) -> tuple[list[dict[str, Any]], bool]:
    with open_db(config.archive.path) as conn:
        if since_sort_key is None:
            rows = conn.execute(
                """
                SELECT sender_name, timestamp, text, sort_key
                FROM messages
                WHERE chat_id = ? AND text IS NOT NULL AND text != ''
                ORDER BY sort_key DESC
                LIMIT ?
                """,
                (chat_id, MAX_CATCHUP_MESSAGES + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT sender_name, timestamp, text, sort_key
                FROM messages
                WHERE chat_id = ? AND sort_key > ? AND text IS NOT NULL AND text != ''
                ORDER BY sort_key DESC
                LIMIT ?
                """,
                (chat_id, since_sort_key, MAX_CATCHUP_MESSAGES + 1),
            ).fetchall()
    truncated = len(rows) > MAX_CATCHUP_MESSAGES
    rows = rows[:MAX_CATCHUP_MESSAGES]
    messages = [
        {
            "sender_name": str(row["sender_name"] or "unknown"),
            "timestamp": str(row["timestamp"] or ""),
            "text": str(row["text"] or ""),
            "sort_key": int(row["sort_key"]),
        }
        for row in reversed(rows)
    ]
    return messages, truncated


def build_catchup_prompt(chat_name: str, messages: list[dict[str, Any]], truncated: bool) -> str:
    lines: list[str] = []
    for message in messages:
        text = " ".join(message["text"].split())
        if len(text) > MAX_LINE_CHARS:
            text = text[: MAX_LINE_CHARS - 3].rstrip() + "..."
        stamp = message["timestamp"][:16].replace("T", " ")
        lines.append(f"- {message['sender_name']} @ {stamp}: {text}")
    thread_block = "\n".join(lines)
    truncation_note = (
        "Only the most recent messages are included; older unseen messages were cut for space.\n"
        if truncated
        else ""
    )
    return (
        f"Summarize what has happened in the group chat '{chat_name}' since the user last caught up.\n"
        "Write a short digest:\n"
        "- group the discussion into its main topics, one bullet per topic\n"
        "- keep concrete details: dates, places, names, decisions, plans\n"
        "- call out anything that was addressed to the user or needs their reply\n"
        "- if nothing substantial happened, say so briefly\n"
        "Do not invent content that is not in the messages.\n"
        "Do not output chain-of-thought. Return only the digest.\n"
        f"{truncation_note}\n"
        f"Messages:\n{thread_block}"
    )


def _summarize(config: AppConfig, prompt: str, llm_client: Any | None) -> str:
    if llm_client is not None and hasattr(llm_client, "summarize_text"):
        return str(llm_client.summarize_text(config, prompt))
    from .llm import OpenAiCompatLlmClient

    client = llm_client if isinstance(llm_client, OpenAiCompatLlmClient) else OpenAiCompatLlmClient()
    return client._post_chat(
        config,
        [
            {"role": "system", "content": "Summarize chat threads faithfully. No hidden reasoning."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max(400, config.llm.max_output_tokens),
        trace_phase="catchup",
    )


def catchup_summary(
    config: AppConfig,
    chat_query: str,
    llm_client: Any | None = None,
    *,
    since_sort_key: int | None = None,
    update_cursor: bool = True,
) -> CatchupResult:
    chat_id, chat_name = resolve_chat(config, chat_query)

    cursor = since_sort_key
    if cursor is None:
        with open_db(config.archive.path) as conn:
            stored = get_runtime_state(conn, _cursor_key(chat_id))
        cursor = int(stored) if stored else None

    messages, truncated = _messages_since(config, chat_id, cursor)
    latest_sort_key = messages[-1]["sort_key"] if messages else cursor
    trace_event("catchup.window", {
        "chat_id": chat_id,
        "chat_name": chat_name,
        "since_sort_key": cursor,
        "message_count": len(messages),
        "truncated": truncated,
    })

    if not messages:
        summary = f"No new messages in {chat_name} since the last catch-up."
    else:
        prompt = build_catchup_prompt(chat_name, messages, truncated)
        trace_event("catchup.prompt", {"chat_id": chat_id, "prompt": prompt})
        summary = _summarize(config, prompt, llm_client).strip()

    if update_cursor and latest_sort_key is not None:
        with open_db(config.archive.path) as conn:
            set_runtime_state(conn, _cursor_key(chat_id), str(latest_sort_key))

    return CatchupResult(
        chat_id=chat_id,
        chat_name=chat_name,
        since_sort_key=cursor,
        latest_sort_key=latest_sort_key,
        message_count=len(messages),
        truncated=truncated,
        summary=summary,
    )


def format_catchup_result(result: CatchupResult) -> str:
    header = f"Catch-up for {result.chat_name}"
    if result.message_count:
        header += f" ({result.message_count} new messages"
        if result.truncated:
            header += ", most recent shown"
        header += ")"
    return f"{header}:\n{result.summary}"
