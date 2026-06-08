from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from .config import AppConfig
from .db import open_db
from .sync import normalize_text


ADDRESS_RE = re.compile(
    r"\b(?:"
    r"\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,5}\s+"
    r"(?:st|street|ave|avenue|rd|road|dr|drive|ln|lane|ct|court|blvd|boulevard|way|pl|place)"
    r"|(?:rua|avenida|av|av\.|estrada|travessa)\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,5}"
    r"(?:\s+n[.°ºo]*\s*\d+)?(?:\s+r/c)?"
    r")\b",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?)\b",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[A-Za-z0-9@._:/+-]+")
STOPWORDS = {
    "a", "an", "about", "and", "are", "at", "be", "been", "did", "do", "for", "from", "have",
    "how", "i", "in", "is", "it", "last", "me", "my", "of", "on", "or", "our", "recent",
    "recently", "say", "saying", "send", "said", "tell", "telling", "that", "the", "their", "there",
    "they", "thing", "things", "to", "told", "us", "was", "what", "when", "where", "who", "why",
    "with", "you", "your",
}
LOW_SIGNAL_TOKENS = {
    "ask", "asked", "get", "got", "say", "said", "send", "sent", "tell", "told",
    "message", "messages", "thing", "things", "last", "recent", "recently", "gave", "give",
}


@dataclass(slots=True)
class SearchResult:
    message_id: str
    chat_id: str
    chat_name: str
    sender_name: str
    timestamp: str
    text: str
    score: float
    match_reasons: list[str]
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchResponse:
    query: str
    results: list[SearchResult]


@dataclass(slots=True)
class SearchCatalog:
    sender_names: list[str]
    chat_names: list[str]


def detect_query_features(query: str) -> list[str]:
    features: list[str] = []
    lowered = query.casefold()
    if ADDRESS_RE.search(query) or "address" in lowered:
        features.append("address")
    if EMAIL_RE.search(query) or "email" in lowered:
        features.append("email")
    if PHONE_RE.search(query) or "phone" in lowered or "call" in lowered:
        features.append("phone")
    if URL_RE.search(query) or any(word in lowered for word in ["url", "link", "site", "website"]):
        features.append("url")
    if DATE_RE.search(query) or any(word in lowered for word in ["date", "day", "month", "year"]):
        features.append("date")
    return features


def _query_tokens(query: str) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(query)
        if len(token) > 1 and token.lower() not in STOPWORDS
    ]


def _fts_query(query: str) -> str:
    tokens = _query_tokens(query)
    if not tokens:
        return ""
    return " OR ".join(f'"{token.replace('"', '""')}"' for token in tokens[:12])


def _date_bounds_from_query(query: str) -> tuple[str | None, str | None]:
    """Try to extract a date range from a natural language query.
    Returns (start_iso, end_iso) or (None, None) if no date found."""
    import re as _re
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    pattern = _re.compile(
        r"(?P<month>" + "|".join(months) + r")(?:\s+)(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:[,\s]+(?P<year>\d{4}))?",
        _re.IGNORECASE,
    )
    match = pattern.search(query)
    if not match:
        # try ISO date
        iso = _re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", query)
        if iso:
            return (iso.group(0) + "T00:00:00Z", iso.group(0) + "T23:59:59Z")
        return (None, None)
    month = months[match.group("month").lower()]
    day = int(match.group("day"))
    year = int(match.group("year")) if match.group("year") else 2026
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    return (date_str + "T00:00:00Z", date_str + "T23:59:59Z")


def _candidate_rows(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    date_start: str | None = None,
    date_end: str | None = None,
    sender_names: list[str] | None = None,
    chat_ids: list[str] | None = None,
) -> list[sqlite3.Row]:
    fts_query = _fts_query(query)
    normalized_query = normalize_text(query) or query.strip()
    rows: list[sqlite3.Row] = []
    filter_clauses: list[str] = []
    filter_params: list[str] = []
    if sender_names:
        filter_clauses.append("COALESCE(m.sender_name, '') IN (" + ", ".join("?" for _ in sender_names) + ")")
        filter_params.extend(sender_names)
    if chat_ids:
        filter_clauses.append("m.chat_id IN (" + ", ".join("?" for _ in chat_ids) + ")")
        filter_params.extend(chat_ids)
    extra_filters = ""
    if filter_clauses:
        extra_filters = "\n                  AND " + "\n                  AND ".join(filter_clauses)

    if fts_query:
        sql = f"""
                SELECT
                    m.message_id,
                    m.chat_id,
                    c.name AS chat_name,
                    COALESCE(m.sender_name, '') AS sender_name,
                    m.timestamp,
                    COALESCE(m.text, '') AS text,
                    bm25(message_fts) AS bm25_score
                FROM message_fts
                JOIN messages AS m ON m.message_id = message_fts.message_id
                JOIN chats AS c ON c.chat_id = m.chat_id
                WHERE message_fts MATCH ?
                  AND (? IS NULL OR m.timestamp >= ?)
                  AND (? IS NULL OR m.timestamp <= ?){extra_filters}
                ORDER BY bm25(message_fts), m.sort_key DESC
                LIMIT ?
                """
        rows = list(
            conn.execute(
                sql,
                (fts_query, date_start, date_start, date_end, date_end, *filter_params, max(limit * 4, 20)),
            )
        )

    if normalized_query:
        like_query = f"%{normalized_query}%"
        seen = {str(row["message_id"]) for row in rows}
        sql = f"""
            SELECT
                m.message_id,
                m.chat_id,
                c.name AS chat_name,
                COALESCE(m.sender_name, '') AS sender_name,
                m.timestamp,
                COALESCE(m.text, '') AS text,
                0.0 AS bm25_score
            FROM messages AS m
            JOIN chats AS c ON c.chat_id = m.chat_id
            WHERE (COALESCE(m.normalized_text, '') LIKE ?
               OR COALESCE(m.sender_name, '') LIKE ?
               OR c.name LIKE ?)
              AND (? IS NULL OR m.timestamp >= ?)
              AND (? IS NULL OR m.timestamp <= ?){extra_filters}
            ORDER BY m.sort_key DESC
            LIMIT ?
            """
        exact_rows = conn.execute(
            sql,
            (like_query, like_query, like_query, date_start, date_start, date_end, date_end, *filter_params, max(limit * 4, 20)),
        )
        for row in exact_rows:
            if str(row["message_id"]) not in seen:
                rows.append(row)
    return rows


def _score_message_fields(
    text: str,
    sender_name: str,
    chat_name: str,
    query: str,
    features: list[str],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    normalized_query = (normalize_text(query) or "").lower()
    normalized_text = (normalize_text(text) or "").lower()

    if normalized_query and normalized_query in normalized_text:
        score += 50.0
        reasons.append("exact-substring")

    lowered_query = query.lower()
    if lowered_query and lowered_query in sender_name.lower():
        score += 20.0
        reasons.append("sender-match")
    if lowered_query and lowered_query in chat_name.lower():
        score += 10.0
        reasons.append("chat-match")

    query_tokens = _query_tokens(query)
    text_tokens = set(token.lower() for token in TOKEN_RE.findall(text))
    strong_overlap = sum(1 for token in query_tokens if token in text_tokens and token not in LOW_SIGNAL_TOKENS)
    weak_overlap = sum(1 for token in query_tokens if token in text_tokens and token in LOW_SIGNAL_TOKENS)
    overlap = strong_overlap + weak_overlap
    if overlap:
        score += strong_overlap * 10.0
        score += weak_overlap * 3.0
        reasons.append("token-overlap")

    if "address" in features and ADDRESS_RE.search(text):
        score += 25.0
        reasons.append("address-shape")
    if "email" in features and EMAIL_RE.search(text):
        score += 25.0
        reasons.append("email-shape")
    if "phone" in features and PHONE_RE.search(text):
        score += 25.0
        reasons.append("phone-shape")
    if "url" in features and URL_RE.search(text):
        score += 25.0
        reasons.append("url-shape")
    if "date" in features and DATE_RE.search(text):
        score += 20.0
        reasons.append("date-shape")

    return score, reasons


def _score_row(row: sqlite3.Row, query: str, features: list[str]) -> tuple[float, list[str]]:
    text = str(row["text"] or "")
    sender_name = str(row["sender_name"] or "")
    chat_name = str(row["chat_name"] or "")
    score, reasons = _score_message_fields(text, sender_name, chat_name, query, features)

    bm25_score = float(row["bm25_score"])
    score += max(0.0, 20.0 - max(bm25_score, 0.0))
    if bm25_score != 0.0:
        reasons.append("fts")

    timestamp = str(row["timestamp"] or "")
    if timestamp[:4].isdigit():
        score += min(float(timestamp[:4]) - 2000.0, 40.0) * 0.05

    return score, reasons


def collect_search_catalog(config: AppConfig, sender_limit: int = 100, chat_limit: int = 100) -> SearchCatalog:
    with open_db(config.archive.path) as conn:
        sender_names = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT sender_name
                FROM messages
                WHERE COALESCE(sender_name, '') != ''
                GROUP BY sender_name
                ORDER BY COUNT(*) DESC, sender_name ASC
                LIMIT ?
                """,
                (sender_limit,),
            )
        ]
        chat_names = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT name
                FROM chats
                WHERE COALESCE(name, '') != ''
                ORDER BY name ASC
                LIMIT ?
                """,
                (chat_limit,),
            )
        ]
    return SearchCatalog(sender_names=sender_names, chat_names=chat_names)


def search_archive(
    config: AppConfig,
    query: str,
    limit: int = 5,
    date_start: str | None = None,
    date_end: str | None = None,
    sender_names: list[str] | None = None,
    chat_ids: list[str] | None = None,
) -> SearchResponse:
    query = query.strip()
    if not query:
        return SearchResponse(query=query, results=[])

    ds, de = date_start, date_end
    if ds is None and de is None:
        ds, de = _date_bounds_from_query(query)

    with open_db(config.archive.path) as conn:
        rows = _candidate_rows(conn, query, limit, ds, de, sender_names=sender_names, chat_ids=chat_ids)

    features = detect_query_features(query)
    scored: list[SearchResult] = []
    for row in rows:
        score, reasons = _score_row(row, query, features)
        scored.append(
            SearchResult(
                message_id=str(row["message_id"]),
                chat_id=str(row["chat_id"]),
                chat_name=str(row["chat_name"]),
                sender_name=str(row["sender_name"]),
                timestamp=str(row["timestamp"]),
                text=str(row["text"]),
                score=score,
                match_reasons=reasons,
            )
        )

    if _query_tokens(query):
        content_reasons = {"token-overlap", "exact-substring", "address-shape", "email-shape", "phone-shape", "url-shape", "date-shape", "fts"}
        distinctive_tokens = [token for token in _query_tokens(query) if token not in LOW_SIGNAL_TOKENS]
        if distinctive_tokens:
            scored = [item for item in scored if any(reason in content_reasons for reason in item.match_reasons)]
        else:
            scored = [item for item in scored if "token-overlap" in item.match_reasons or item.match_reasons]
    scored.sort(key=lambda item: item.timestamp, reverse=True)
    scored.sort(key=lambda item: item.score, reverse=True)
    return SearchResponse(query=query, results=scored[:limit])


def expand_results_with_context(config: AppConfig, results: list[SearchResult], window: int = 3) -> list[SearchResult]:
    if not results or window <= 0:
        return list(results)

    with open_db(config.archive.path) as conn:
        for result in results:
            anchor = conn.execute(
                "SELECT sort_key FROM messages WHERE message_id = ?",
                (result.message_id,),
            ).fetchone()
            if anchor is None:
                continue

            sort_key = int(anchor[0])
            before = conn.execute(
                """
                SELECT COALESCE(sender_name, '') AS sender_name, timestamp, COALESCE(text, '') AS text
                FROM messages
                WHERE chat_id = ? AND sort_key < ?
                ORDER BY sort_key DESC
                LIMIT ?
                """,
                (result.chat_id, sort_key, window),
            ).fetchall()
            after = conn.execute(
                """
                SELECT COALESCE(sender_name, '') AS sender_name, timestamp, COALESCE(text, '') AS text
                FROM messages
                WHERE chat_id = ? AND sort_key > ?
                ORDER BY sort_key ASC
                LIMIT ?
                """,
                (result.chat_id, sort_key, window),
            ).fetchall()

            result.context_before = [
                f"{str(row['sender_name'] or 'unknown')} @ {str(row['timestamp'])}: {str(row['text']).replace(chr(10), ' ').strip()}"
                for row in reversed(before)
                if str(row['text'] or '').strip()
            ]
            result.context_after = [
                f"{str(row['sender_name'] or 'unknown')} @ {str(row['timestamp'])}: {str(row['text']).replace(chr(10), ' ').strip()}"
                for row in after
                if str(row['text'] or '').strip()
            ]

    return list(results)


def _sort_results(results: list[SearchResult], answer_kind: str) -> list[SearchResult]:
    if answer_kind == "last-message":
        results.sort(key=lambda item: item.score, reverse=True)
        results.sort(key=lambda item: item.timestamp, reverse=True)
    else:
        results.sort(key=lambda item: item.timestamp, reverse=True)
        results.sort(key=lambda item: item.score, reverse=True)
    return results


def expand_results_with_spans(
    config: AppConfig,
    query: str,
    results: list[SearchResult],
    *,
    answer_kind: str = "fact",
    window: int = 8,
    seed_limit: int = 4,
) -> list[SearchResult]:
    if not results or window <= 0:
        return list(results)

    features = detect_query_features(query)
    merged: dict[str, SearchResult] = {item.message_id: item for item in results}

    with open_db(config.archive.path) as conn:
        for seed in results[:seed_limit]:
            anchor = conn.execute(
                "SELECT sort_key FROM messages WHERE message_id = ?",
                (seed.message_id,),
            ).fetchone()
            if anchor is None:
                continue

            sort_key = int(anchor[0])
            rows = conn.execute(
                """
                SELECT
                    m.message_id,
                    m.chat_id,
                    c.name AS chat_name,
                    COALESCE(m.sender_name, '') AS sender_name,
                    m.timestamp,
                    m.sort_key,
                    COALESCE(m.text, '') AS text
                FROM messages AS m
                JOIN chats AS c ON c.chat_id = m.chat_id
                WHERE m.chat_id = ?
                  AND m.sort_key BETWEEN ? AND ?
                ORDER BY m.sort_key ASC
                """,
                (seed.chat_id, sort_key - window, sort_key + window),
            ).fetchall()

            for row in rows:
                text = str(row["text"] or "").strip()
                if not text:
                    continue

                message_id = str(row["message_id"])
                distance = abs(int(row["sort_key"]) - sort_key)
                field_score, field_reasons = _score_message_fields(
                    text,
                    str(row["sender_name"] or ""),
                    str(row["chat_name"] or ""),
                    query,
                    features,
                )
                same_sender = str(row["sender_name"] or "").casefold() == seed.sender_name.casefold()
                if message_id != seed.message_id and field_score <= 0 and distance > 3 and not same_sender:
                    continue

                score = max(seed.score * 0.35, 0.0) + field_score + max(0.0, 18.0 - (distance * 2.0))
                reasons = sorted(set(field_reasons + ["span-nearby"]))
                current = merged.get(message_id)
                if current is None or score > current.score:
                    merged[message_id] = SearchResult(
                        message_id=message_id,
                        chat_id=str(row["chat_id"]),
                        chat_name=str(row["chat_name"]),
                        sender_name=str(row["sender_name"]),
                        timestamp=str(row["timestamp"]),
                        text=text,
                        score=score,
                        match_reasons=reasons,
                    )
                elif "span-nearby" not in current.match_reasons:
                    current.match_reasons = sorted(set(current.match_reasons + reasons))

    return _sort_results(list(merged.values()), answer_kind)


def search_archive_multi(
    config: AppConfig,
    queries: list[str],
    limit: int = 10,
    preferred_senders: list[str] | None = None,
    preferred_chats: list[str] | None = None,
    answer_kind: str = "fact",
    time_hint: str = "any",
    restrict_chats: list[str] | None = None,
    restrict_senders: list[str] | None = None,
) -> SearchResponse:
    merged: dict[str, SearchResult] = {}
    preferred_senders_cf = {value.casefold() for value in preferred_senders or [] if value.strip()}
    preferred_chats_cf = {value.casefold() for value in preferred_chats or [] if value.strip()}
    restrict_chats_cf = {value.casefold() for value in restrict_chats or [] if value.strip()}
    restrict_senders_cf = {value.casefold() for value in restrict_senders or [] if value.strip()}

    per_query_limit = max(limit, 8)
    if restrict_chats_cf or restrict_senders_cf or answer_kind in {"last-message", "url"}:
        per_query_limit = max(limit * 5, 40)

    for query in queries:
        response = search_archive(
            config,
            query,
            limit=per_query_limit,
            sender_names=restrict_senders,
            chat_ids=restrict_chats,
        )
        for result in response.results:
            if restrict_chats_cf and result.chat_id.casefold() not in restrict_chats_cf:
                continue
            if restrict_senders_cf and result.sender_name.casefold() not in restrict_senders_cf:
                continue
            score = result.score
            reasons = list(result.match_reasons)
            if result.sender_name.casefold() in preferred_senders_cf:
                score += 20.0
                reasons.append("preferred-sender")
            if result.chat_id.casefold() in preferred_chats_cf or result.chat_name.casefold() in preferred_chats_cf:
                score += 15.0
                reasons.append("preferred-chat")
            if time_hint == "recent":
                score += 10.0
                reasons.append("recent-hint")
            if answer_kind == "date" and DATE_RE.search(result.text):
                score += 15.0
                reasons.append("date-kind")
            if answer_kind == "url" and URL_RE.search(result.text):
                score += 15.0
                reasons.append("url-kind")
            if answer_kind == "last-message":
                score += 12.0
                reasons.append("last-message-kind")
            current = merged.get(result.message_id)
            if current is None or score > current.score:
                merged[result.message_id] = SearchResult(
                    message_id=result.message_id,
                    chat_id=result.chat_id,
                    chat_name=result.chat_name,
                    sender_name=result.sender_name,
                    timestamp=result.timestamp,
                    text=result.text,
                    score=score,
                    match_reasons=sorted(set(reasons)),
                )
            else:
                current.score += 2.0
                current.match_reasons = sorted(set(current.match_reasons + reasons + ["multi-query"] ))

    results = _sort_results(list(merged.values()), answer_kind)
    return SearchResponse(query=" | ".join(queries), results=results[:limit])


def format_find_response(response: SearchResponse) -> str:
    if not response.results:
        return f"No matches found for: {response.query}"

    lines = [f"Top matches for: {response.query}"]
    for idx, result in enumerate(response.results, start=1):
        excerpt = result.text.replace("\n", " ").strip()
        if len(excerpt) > 160:
            excerpt = excerpt[:157].rstrip() + "..."
        lines.append(
            f"{idx}. [{result.chat_name}] {result.sender_name or 'unknown'} @ {result.timestamp}"
        )
        lines.append(f"   {excerpt}")
    return "\n".join(lines)
