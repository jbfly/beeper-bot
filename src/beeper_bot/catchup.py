from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, ChatSetConfig
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


@dataclass(slots=True)
class ResolvedChatSelection:
    chats: list[tuple[str, str]]
    display_name: str | None = None
    is_chat_set: bool = False


def _fuzzy_score(query: str, title: str) -> float:
    """Whole-string similarity, boosted by per-token close matches so
    misspellings like 'sample volunters' or 'neighborhood' still resolve."""
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


def _chat_rows(config: AppConfig):
    with open_db(config.archive.path) as conn:
        return conn.execute(
            """
            SELECT c.chat_id, c.name, MAX(m.sort_key) AS latest
            FROM chats c
            LEFT JOIN messages m ON m.chat_id = c.chat_id
            WHERE c.is_allowed = 1
            GROUP BY c.chat_id, c.name
            """
        ).fetchall()


def _normalize_set_label(value: str) -> str:
    words = re.findall(r"[\w'À-ÿ]+", value.replace("_", " "))
    return " ".join(words).casefold()


def _configured_chat_set(config: AppConfig, chat_query: str) -> ChatSetConfig | None:
    query = _normalize_set_label(chat_query)
    if not query:
        return None
    substring_match: ChatSetConfig | None = None
    for chat_set in config.chat_sets.values():
        labels = [chat_set.name, chat_set.display_name, *chat_set.aliases]
        for label in labels:
            normalized = _normalize_set_label(label)
            if not normalized:
                continue
            if query == normalized:
                return chat_set
            query_words = query.split()
            label_words = normalized.split()
            if len(query_words) >= 2 and len(label_words) >= 2 and (query in normalized or normalized in query):
                substring_match = substring_match or chat_set
    return substring_match


def _select_configured_chats(config: AppConfig, chat_set: ChatSetConfig, limit: int) -> list[tuple[str, str]]:
    rows = _chat_rows(config)
    selected: list[tuple[int, str, str]] = []
    seen: set[str] = set()

    def add(row: Any) -> None:
        chat_id = str(row["chat_id"])
        if chat_id in seen:
            return
        seen.add(chat_id)
        selected.append((int(row["latest"] or 0), chat_id, str(row["name"] or chat_id)))

    for entry in chat_set.chats:
        wanted = entry.strip()
        if not wanted:
            continue
        lowered = wanted.casefold()
        exact_id = [row for row in rows if str(row["chat_id"]) == wanted]
        exact_title = [row for row in rows if str(row["name"] or "").casefold() == lowered]
        substring = [row for row in rows if lowered and lowered in str(row["name"] or "").casefold()]
        matches = exact_id or exact_title or substring
        if not matches:
            matches = [row for row in rows if _fuzzy_score(lowered, str(row["name"] or "").casefold()) >= FUZZY_MATCH_CUTOFF]
        matches.sort(key=lambda row: int(row["latest"] or 0), reverse=True)
        for row in matches:
            add(row)
            if len(selected) >= limit:
                break
        if len(selected) >= limit:
            break
    selected.sort(reverse=True)
    return [(chat_id, name) for _, chat_id, name in selected[: max(1, limit)]]


def _resolve_chats_by_title(config: AppConfig, chat_query: str, limit: int) -> list[tuple[str, str]]:
    """All archive chats matching a title query, best first. Exact and
    substring matches win; token and fuzzy matches cover typos."""
    query = chat_query.strip().casefold()
    if not query:
        raise CatchupError("Chat name is required")
    rows = _chat_rows(config)
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
    # Fuzzy hits ride along with literal ones: 'sample' must collect the
    # 'Sample' chats even though Sample Volunteers matches literally.
    matches = scored + fuzzy
    if not matches:
        raise CatchupError(f"No indexed chat matches '{chat_query.strip()}'")
    matches.sort(reverse=True)
    return [(chat_id, name) for _, _, chat_id, name in matches[: max(1, limit)]]


def resolve_chat_selection(config: AppConfig, chat_query: str, limit: int = MAX_CATCHUP_CHATS) -> ResolvedChatSelection:
    chat_set = _configured_chat_set(config, chat_query)
    if chat_set is not None:
        chats = _select_configured_chats(config, chat_set, max(1, limit))
        if chats:
            return ResolvedChatSelection(chats=chats, display_name=chat_set.display_name, is_chat_set=True)
    return ResolvedChatSelection(chats=_resolve_chats_by_title(config, chat_query, limit))


def resolve_chats(config: AppConfig, chat_query: str, limit: int = MAX_CATCHUP_CHATS) -> list[tuple[str, str]]:
    return resolve_chat_selection(config, chat_query, limit).chats


def resolve_chat(config: AppConfig, chat_query: str) -> tuple[str, str]:
    return resolve_chats(config, chat_query, limit=1)[0]


def _cursor_key(chat_id: str) -> str:
    return f"{CATCHUP_CURSOR_PREFIX}{chat_id}"


def _messages_since(config: AppConfig, chat_id: str, since_sort_key: int | None) -> tuple[list[dict[str, Any]], bool]:
    with open_db(config.archive.path) as conn:
        if since_sort_key is None:
            rows = conn.execute(
                """
                SELECT m.sender_name, m.timestamp, m.text, m.sort_key
                FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                WHERE m.chat_id = ? AND c.is_allowed = 1 AND m.text IS NOT NULL AND m.text != ''
                ORDER BY m.sort_key DESC
                LIMIT ?
                """,
                (chat_id, MAX_CATCHUP_MESSAGES + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT m.sender_name, m.timestamp, m.text, m.sort_key
                FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                WHERE m.chat_id = ? AND c.is_allowed = 1 AND m.sort_key > ? AND m.text IS NOT NULL AND m.text != ''
                ORDER BY m.sort_key DESC
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
    """Digest one chat, or all chats matching the query (e.g. 'neighborhood'
    matches every Neighborhood group). Cursors advance per chat."""
    selection = resolve_chat_selection(config, chat_query)
    chats = selection.chats

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

    if selection.is_chat_set and selection.display_name:
        if len(chats) == 1:
            display_name = f"{selection.display_name}: {chats[0][1]}"
        else:
            display_name = f"{selection.display_name} ({len(chats)} chats: " + ", ".join(name for _, name in chats) + ")"
    else:
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
    r"\b(?:summar\w*|recap|tl;?dr|gist|overview|catch\s+(?:me\s+)?up|(?:what'?s|what\s+is)\s+(?:been\s+)?(?:happening|going\s+on|new)|update\s+me)\b",
    re.IGNORECASE,
)
CHAT_NOUN_RE = re.compile(r"^(?:group\s+)?(?:chats?|groups?|channels?)\b", re.IGNORECASE)
CHAT_NAME_BEFORE_NOUN_RE = re.compile(
    r"([\w'&@.\-À-ÿ ]{2,60}?)\s+(?:group\s+)?(?:chats?|groups?|channels?)\b",
    re.IGNORECASE,
)
HAPPENING_IN_RE = re.compile(
    r"\b(?:what'?s|what\s+is)\s+(?:been\s+)?(?:happening|going\s+on)\s+(?:in|around|with|about)\s+([^?!.]+)",
    re.IGNORECASE,
)
UPDATE_ON_RE = re.compile(
    r"\b(?:catch\s+(?:me\s+)?up|update\s+me)\s+(?:on|about|with)\s+([^?!.]+)",
    re.IGNORECASE,
)
SUMMARY_AROUND_RE = re.compile(
    r"\b(?:summar\w*|recap|overview|gist)\b.{0,40}\b(?:in|around|about)\s+([^?!.]+)",
    re.IGNORECASE,
)
RELATED_TO_RE = re.compile(
    r"\b(?:related\s+to|about)\s+([^?!.]+)",
    re.IGNORECASE,
)
_DIGEST_STOPWORDS = {
    "summary", "summarize", "summarise", "recap", "overview", "gist", "give", "get", "me", "a", "an",
    "of", "the", "all", "my", "our", "those", "these", "related", "latest", "recent", "new", "please",
    "can", "you", "could", "in", "on", "for", "from", "up", "catch", "whats", "what's", "happening",
    "going", "been", "what", "is", "update",
}


def _clean_digest_query(value: str) -> str | None:
    tokens = value.split()
    while tokens and tokens[0].casefold().strip("?,.!") in _DIGEST_STOPWORDS:
        tokens.pop(0)
    query = " ".join(tokens).strip(" ?.!,")
    query = re.sub(r"^(?:group\s+)?(?:chats?|groups?|channels?)\s+(?:related\s+to|about)\s+", "", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+(?:group\s+)?(?:chats?|groups?|channels?)\s*$", "", query, flags=re.IGNORECASE).strip(" ?.!,")
    return query or None


def parse_chat_digest_request(question: str) -> str | None:
    """Detect digest requests and return the chat or chat-set query.
    Handles explicit chat nouns plus natural forms like 'what is happening
    in Neighborhood'. Generic shapes only; memo/address questions fall
    through to their own routes."""
    if not DIGEST_INTENT_RE.search(question):
        return None
    match = CHAT_NAME_BEFORE_NOUN_RE.search(question)
    if match:
        query = _clean_digest_query(match.group(1))
        if query:
            return query
    for pattern in (HAPPENING_IN_RE, UPDATE_ON_RE, SUMMARY_AROUND_RE, RELATED_TO_RE):
        match = pattern.search(question)
        if not match:
            continue
        query = _clean_digest_query(match.group(1))
        if query:
            return query
    return None


def format_catchup_result(result: CatchupResult) -> str:
    header = f"Catch-up for {result.chat_name}"
    if result.message_count:
        header += f" ({result.message_count} new messages"
        if result.truncated:
            header += ", most recent shown"
        header += ")"
    return f"{header}:\n{result.summary}"
