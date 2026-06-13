from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .db import get_runtime_state, open_db, set_runtime_state, utc_now
from .tracing import trace_event


CATCHUP_CURSOR_PREFIX = "catchup_cursor:"
MAX_CATCHUP_MESSAGES = 300
MAX_CATCHUP_MESSAGES_PER_CHAT = 120
MAX_CATCHUP_CHATS = 8
MAX_LINE_CHARS = 280
FUZZY_MATCH_CUTOFF = 0.72


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


def _fuzzy_score(query: str, title: str) -> float:
    """Whole-string similarity, boosted by per-token close matches so
    misspellings like 'boomerangatangs' or 'bom successo' still resolve."""
    whole = difflib.SequenceMatcher(None, query, title).ratio()
    title_tokens = [token for token in re.findall(r"[\w'À-ÿ]+", title) if len(token) >= 3]
    query_tokens = [token for token in re.findall(r"[\w'À-ÿ]+", query) if len(token) >= 3]
    if not query_tokens or not title_tokens:
        return whole
    token_hits = sum(
        1 for q in query_tokens
        if difflib.get_close_matches(q, title_tokens, n=1, cutoff=FUZZY_MATCH_CUTOFF)
    )
    token_score = token_hits / len(query_tokens)
    return max(whole, token_score)


def resolve_chats(config: AppConfig, chat_query: str, limit: int = MAX_CATCHUP_CHATS) -> list[tuple[str, str]]:
    """All archive chats matching a title query, best first. Exact and
    substring matches win; token and fuzzy matches cover typos."""
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
    scored: list[tuple[float, int, str, str]] = []
    fuzzy: list[tuple[float, int, str, str]] = []
    for row in rows:
        name = str(row["name"] or "")
        lowered = name.casefold()
        latest = int(row["latest"] or 0)
        chat_id = str(row["chat_id"])
        if query == lowered:
            scored.append((3.0, latest, chat_id, name))
        elif query in lowered:
            scored.append((2.0, latest, chat_id, name))
        elif all(token in lowered for token in query.split() if len(token) >= 3):
            scored.append((1.0, latest, chat_id, name))
        else:
            ratio = _fuzzy_score(query, lowered)
            if ratio >= FUZZY_MATCH_CUTOFF:
                fuzzy.append((ratio, latest, chat_id, name))
    # Fuzzy hits ride along with literal ones: 'boom' must collect the
    # 'Booom' chats even though Boomerangutans matches literally.
    matches = scored + fuzzy
    if not matches:
        raise CatchupError(f"No indexed chat matches '{chat_query.strip()}'")
    matches.sort(reverse=True)
    return [(chat_id, name) for _, _, chat_id, name in matches[: max(1, limit)]]


def resolve_chat(config: AppConfig, chat_query: str) -> tuple[str, str]:
    return resolve_chats(config, chat_query, limit=1)[0]


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
        "- group the discussion into its main topics\n"
        "- keep concrete details: dates, places, names, decisions, plans\n"
        "- call out anything that was addressed to the user or needs their reply\n"
        "- if nothing substantial happened, say so briefly\n"
        "Format for a plain-text chat app, not markdown: no #, *, or ** symbols. "
        "Start each topic with an emoji that fits it and a short title on its own line, "
        "use '• ' for bullet points, and leave a blank line between topics.\n"
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


def build_multi_catchup_prompt(sections: list[tuple[str, list[dict[str, Any]], bool]]) -> str:
    blocks: list[str] = []
    for chat_name, messages, truncated in sections:
        lines: list[str] = []
        for message in messages:
            text = " ".join(message["text"].split())
            if len(text) > MAX_LINE_CHARS:
                text = text[: MAX_LINE_CHARS - 3].rstrip() + "..."
            stamp = message["timestamp"][:16].replace("T", " ")
            lines.append(f"- {message['sender_name']} @ {stamp}: {text}")
        note = " (only the most recent messages shown)" if truncated else ""
        blocks.append(f"## {chat_name}{note}\n" + "\n".join(lines))
    thread_block = "\n\n".join(blocks)
    return (
        "Summarize what has happened in these related group chats since the user last caught up.\n"
        "Write a digest with one short section per chat:\n"
        "- group each chat's discussion into its main topics\n"
        "- keep concrete details: dates, places, names, decisions, plans\n"
        "- call out anything addressed to the user or needing their reply\n"
        "- if a chat had nothing substantial, one line is enough\n"
        "Format for a plain-text chat app, not markdown: no #, *, or ** symbols. "
        "Start each chat's section with '💬 ' and the chat name on its own line, "
        "use '• ' for bullet points, and leave a blank line between sections and topics.\n"
        "Do not invent content that is not in the messages.\n"
        "Do not output chain-of-thought. Return only the digest.\n\n"
        f"Chats:\n{thread_block}"
    )


def catchup_summary(
    config: AppConfig,
    chat_query: str,
    llm_client: Any | None = None,
    *,
    since_sort_key: int | None = None,
    update_cursor: bool = True,
) -> CatchupResult:
    """Digest one chat, or all chats matching the query (e.g. 'bom sucesso'
    matches every Bom Sucesso group). Cursors advance per chat."""
    chats = resolve_chats(config, chat_query)

    sections: list[tuple[str, list[dict[str, Any]], bool]] = []
    quiet: list[str] = []
    cursors: dict[str, int | None] = {}
    new_cursors: dict[str, int] = {}
    per_chat_cap = MAX_CATCHUP_MESSAGES if len(chats) == 1 else MAX_CATCHUP_MESSAGES_PER_CHAT
    total_messages = 0
    any_truncated = False

    for chat_id, chat_name in chats:
        cursor = since_sort_key
        if cursor is None:
            with open_db(config.archive.path) as conn:
                stored = get_runtime_state(conn, _cursor_key(chat_id))
            cursor = int(stored) if stored else None
        cursors[chat_id] = cursor
        messages, truncated = _messages_since(config, chat_id, cursor)
        if len(messages) > per_chat_cap:
            messages = messages[-per_chat_cap:]
            truncated = True
        trace_event("catchup.window", {
            "chat_id": chat_id,
            "chat_name": chat_name,
            "since_sort_key": cursor,
            "message_count": len(messages),
            "truncated": truncated,
        })
        if messages:
            sections.append((chat_name, messages, truncated))
            new_cursors[chat_id] = messages[-1]["sort_key"]
            total_messages += len(messages)
            any_truncated = any_truncated or truncated
        else:
            quiet.append(chat_name)

    display_name = chats[0][1] if len(chats) == 1 else f"{len(chats)} chats: " + ", ".join(name for _, name in chats)

    if not sections:
        summary = f"No new messages in {display_name} since the last catch-up."
    else:
        if len(sections) == 1 and len(chats) == 1:
            prompt = build_catchup_prompt(sections[0][0], sections[0][1], sections[0][2])
        else:
            prompt = build_multi_catchup_prompt(sections)
        trace_event("catchup.prompt", {"chats": [name for name, _, _ in sections], "prompt": prompt})
        summary = _summarize(config, prompt, llm_client).strip()
        if quiet:
            summary += f"\n\nNo new activity in: {', '.join(quiet)}."

    if update_cursor:
        with open_db(config.archive.path) as conn:
            for chat_id, latest in new_cursors.items():
                set_runtime_state(conn, _cursor_key(chat_id), str(latest))

    first_chat_id = chats[0][0]
    return CatchupResult(
        chat_id=first_chat_id if len(chats) == 1 else ",".join(chat_id for chat_id, _ in chats),
        chat_name=display_name,
        since_sort_key=cursors.get(first_chat_id),
        latest_sort_key=new_cursors.get(first_chat_id, cursors.get(first_chat_id)),
        message_count=total_messages,
        truncated=any_truncated,
        summary=summary,
    )


DIGEST_INTENT_RE = re.compile(
    r"\b(?:summar\w*|recap|tl;?dr|gist|overview|catch\s+(?:me\s+)?up|what'?s\s+(?:been\s+)?(?:happening|going\s+on|new)|update\s+me)\b",
    re.IGNORECASE,
)
CHAT_NOUN_RE = re.compile(r"^(?:group\s+)?(?:chats?|groups?|channels?)\b", re.IGNORECASE)
CHAT_NAME_BEFORE_NOUN_RE = re.compile(
    r"([\w'&@.\-À-ÿ ]{2,60}?)\s+(?:group\s+)?(?:chats?|groups?|channels?)\b",
    re.IGNORECASE,
)
_DIGEST_STOPWORDS = {
    "summary", "summarize", "summarise", "recap", "overview", "gist", "give", "get", "me", "a", "an",
    "of", "the", "all", "my", "our", "those", "these", "related", "latest", "recent", "new", "please",
    "can", "you", "could", "in", "on", "for", "from", "up", "catch", "whats", "what's", "happening",
    "going", "been", "what", "is", "update",
}


def parse_chat_digest_request(question: str) -> str | None:
    """Detect 'summarize the X chat(s)' shapes and return the chat-name
    query. Generic command shapes only; returns None when no digest intent
    or no chat noun is present."""
    if not DIGEST_INTENT_RE.search(question):
        return None
    match = CHAT_NAME_BEFORE_NOUN_RE.search(question)
    if not match:
        return None
    tokens = match.group(1).split()
    while tokens and tokens[0].casefold().strip("?,.!") in _DIGEST_STOPWORDS:
        tokens.pop(0)
    query = " ".join(tokens).strip(" ?.!,")
    return query or None


def format_catchup_result(result: CatchupResult) -> str:
    header = f"Catch-up for {result.chat_name}"
    if result.message_count:
        header += f" ({result.message_count} new messages"
        if result.truncated:
            header += ", most recent shown"
        header += ")"
    return f"{header}:\n{result.summary}"
