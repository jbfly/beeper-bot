from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import AppConfig
from .db import get_runtime_state, open_db, set_runtime_state


DYNAMIC_CHAT_IDS_KEY = "dynamic_indexed_chat_ids"

# Titles that are service feeds rather than conversations: bare phone
# numbers, SMS short codes, and one-time-code senders.
NOISE_TITLE_RE = re.compile(r"^[\d\s()+\-#*]{3,}$")


def _title(chat: dict[str, Any]) -> str:
    return str(chat.get("title") or "").strip()


def is_noise_chat(chat: dict[str, Any]) -> bool:
    title = _title(chat)
    if not title:
        return True
    if NOISE_TITLE_RE.match(title):
        return True
    if chat.get("isArchived"):
        return True
    return False


def _last_activity(chat: dict[str, Any]) -> datetime | None:
    raw = str(chat.get("lastActivity") or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def recent_chat_ids(all_chats: list[dict[str, Any]], days: int, max_chats: int) -> list[str]:
    """Chats with activity within `days`, newest first, noise feeds skipped."""
    if days <= 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    candidates: list[tuple[datetime, str]] = []
    for chat in all_chats:
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id or is_noise_chat(chat):
            continue
        stamp = _last_activity(chat)
        if stamp is None or stamp < cutoff:
            continue
        candidates.append((stamp, chat_id))
    candidates.sort(reverse=True)
    return [chat_id for _, chat_id in candidates[: max(1, max_chats)]]


def dynamic_indexed_chat_ids(config: AppConfig) -> list[str]:
    with open_db(config.archive.path) as conn:
        raw = get_runtime_state(conn, DYNAMIC_CHAT_IDS_KEY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def add_dynamic_indexed_chat_ids(config: AppConfig, chat_ids: list[str]) -> list[str]:
    current = dynamic_indexed_chat_ids(config)
    merged = list(dict.fromkeys(current + [chat_id.strip() for chat_id in chat_ids if chat_id.strip()]))
    with open_db(config.archive.path) as conn:
        set_runtime_state(conn, DYNAMIC_CHAT_IDS_KEY, json.dumps(merged))
    return merged


def effective_indexed_chat_ids(config: AppConfig, all_chats: list[dict[str, Any]] | None = None) -> list[str]:
    """Configured allowlist plus dynamically added chats plus, when
    auto-indexing is enabled and a chat listing is provided, recently
    active chats."""
    ids = list(config.beeper.indexed_chat_ids)
    ids.extend(dynamic_indexed_chat_ids(config))
    if all_chats and config.beeper.auto_index_recent_days > 0:
        ids.extend(
            recent_chat_ids(
                all_chats,
                config.beeper.auto_index_recent_days,
                config.beeper.auto_index_max_chats,
            )
        )
    return list(dict.fromkeys(ids))


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-zÀ-ÿ0-9'&-]+", text)
        if len(token) >= 3
    }


def match_unindexed_chats(
    question: str,
    all_chats: list[dict[str, Any]],
    indexed_ids: set[str],
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Find chats the question names that are not yet in the archive.

    A chat matches when its full title appears in the question, or when all
    of its title tokens appear (short titles), or at least two tokens appear
    (longer titles). Generic linguistic matching only — no per-question
    rules."""
    question_lower = question.casefold()
    question_tokens = _tokens(question)
    matches: list[tuple[int, dict[str, Any]]] = []
    for chat in all_chats:
        chat_id = str(chat.get("id") or "").strip()
        title = _title(chat)
        if not chat_id or chat_id in indexed_ids or is_noise_chat(chat):
            continue
        title_lower = title.casefold()
        title_tokens = _tokens(title)
        if not title_tokens:
            continue
        score = 0
        if title_lower and title_lower in question_lower:
            score = 100 + len(title_lower)
        else:
            overlap = len(title_tokens & question_tokens)
            if overlap == len(title_tokens) and (len(title_tokens) > 1 or len(next(iter(title_tokens))) >= 4):
                score = 50 + overlap
            elif len(title_tokens) >= 3 and overlap >= 2:
                score = overlap
        if score > 0:
            matches.append((score, chat))
    matches.sort(key=lambda item: -item[0])
    return [chat for _, chat in matches[: max(1, limit)]]
