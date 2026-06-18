from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request

from .config import AppConfig, ensure_private_dir
from .db import open_db, utc_now
from .sync import normalize_text
from .tracing import trace_event


AUDIO_CHUNK_SECONDS = 28
AUDIO_CHUNK_OVERLAP_SECONDS = 2
MAX_AUDIO_CHUNKS = 20
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

VOICE_PREFIX = "[voice memo transcript]"
IMAGE_PREFIX = "[image]"


class MediaError(RuntimeError):
    pass


@dataclass(slots=True)
class DerivedResult:
    message_id: str
    chat_id: str
    kind: str
    status: str
    derived_text: str
    chunk_count: int = 0
    duration_seconds: float | None = None
    error_text: str = ""


class MediaLlmClient(Protocol):
    def transcribe_audio_wav(self, config: AppConfig, wav_bytes: bytes) -> str: ...
    def describe_image(self, config: AppConfig, image_bytes: bytes, mime_type: str) -> str: ...


class OpenAiCompatMediaClient:
    """Multimodal calls to the local llama-server. Separate from the text
    client so trace events carry metadata only, never base64 payloads."""

    def _post(self, config: AppConfig, content: list[dict[str, Any]], max_tokens: int, phase: str) -> str:
        payload = {
            "model": config.llm.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        url = f"{config.llm.base_url.rstrip('/')}/chat/completions"
        trace_event(f"{phase}.request", {"url": url, "parts": [part.get("type") for part in content]})
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=max(config.llm.timeout_seconds, 240)) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise MediaError(f"Media LLM call failed: HTTP {exc.code} {exc.read().decode('utf-8', errors='replace')[:200]}") from exc
        except error.URLError as exc:
            raise MediaError(f"Media LLM call failed: {exc}") from exc
        try:
            text = str(body["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise MediaError("Media LLM call returned an unexpected response") from exc
        trace_event(f"{phase}.response", {"text": text})
        return text

    def transcribe_audio_wav(self, config: AppConfig, wav_bytes: bytes) -> str:
        content = [
            {"type": "text", "text": "Transcribe this voice memo verbatim. Output only the transcript."},
            {
                "type": "input_audio",
                "input_audio": {"data": base64.b64encode(wav_bytes).decode("ascii"), "format": "wav"},
            },
        ]
        return self._post(config, content, max_tokens=600, phase="media.transcribe")

    def describe_image(self, config: AppConfig, image_bytes: bytes, mime_type: str) -> str:
        data_uri = f"data:{mime_type or 'image/jpeg'};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        content = [
            {
                "type": "text",
                "text": (
                    "Index this chat image for a searchable archive. Text comes first:\n"
                    "- if it is a flyer, poster, screenshot, sign, document, menu, schedule, or receipt, "
                    "transcribe ALL readable text verbatim — names, dates, times, places, prices, phone numbers, URLs\n"
                    "- then add one short sentence of visual context\n"
                    "- if there is no readable text, describe the image in one sentence\n"
                    "Do not invent text that is not clearly visible."
                ),
            },
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
        return self._post(config, content, max_tokens=350, phase="media.describe")


def _media_cache_dir(config: AppConfig) -> Path:
    cache = config.state_dir / "media-cache"
    ensure_private_dir(cache)
    return cache


def fetch_attachment(config: AppConfig, src_url: str) -> Path:
    """Resolve an attachment to a local file. file:// URLs point straight at
    Beeper Desktop's media store; mxc:// URLs are fetched (and decrypted) by
    the Desktop API's /v1/assets/serve endpoint."""
    src_url = src_url.strip()
    if src_url.startswith("file://"):
        path = Path(urllib.parse.unquote(src_url[7:]))
        if not path.exists():
            raise MediaError(f"Attachment file missing: {path}")
        return path
    if not src_url.startswith("mxc://"):
        raise MediaError(f"Unsupported attachment URL scheme: {src_url[:40]}")

    cache_path = _media_cache_dir(config) / hashlib.sha256(src_url.encode()).hexdigest()[:32]
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    from .beeper_api import BeeperApiClient

    client = BeeperApiClient(config.beeper)
    token = client._load_token()
    url = f"{config.beeper.api_base.rstrip('/')}/assets/serve?url={urllib.parse.quote(src_url, safe='')}"
    req = request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with request.urlopen(req, timeout=max(config.beeper.http_timeout_seconds, 120)) as resp:
            data = resp.read()
    except error.HTTPError as exc:
        raise MediaError(f"Asset download failed: HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise MediaError(f"Asset download failed: {exc}") from exc
    if not data:
        raise MediaError("Asset download returned no data")
    cache_path.write_bytes(data)
    return cache_path


def _ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed: {proc.stderr.strip()[:200]}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise MediaError(f"ffprobe returned no duration: {proc.stdout.strip()[:80]}") from exc


def _audio_chunk_wav(path: Path, offset: float, length: float) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{offset:.2f}", "-t", f"{length:.2f}",
                "-i", str(path),
                "-ar", "16000", "-ac", "1",
                tmp.name,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise MediaError(f"ffmpeg failed: {proc.stderr.strip()[:200]}")
        return Path(tmp.name).read_bytes()


def transcribe_voice_memo(config: AppConfig, path: Path, llm_client: MediaLlmClient | None = None) -> tuple[str, int, float]:
    """Transcribe an audio file in bounded chunks. Returns (text, chunk_count, duration)."""
    client = llm_client or OpenAiCompatMediaClient()
    duration = _ffprobe_duration(path)
    step = AUDIO_CHUNK_SECONDS - AUDIO_CHUNK_OVERLAP_SECONDS
    offsets = [i * step for i in range(MAX_AUDIO_CHUNKS) if i * step < duration]
    parts: list[str] = []
    for offset in offsets:
        wav = _audio_chunk_wav(path, offset, AUDIO_CHUNK_SECONDS)
        text = client.transcribe_audio_wav(config, wav).strip()
        if text:
            parts.append(text)
    transcript = " ".join(parts).strip()
    if offsets and duration > offsets[-1] + AUDIO_CHUNK_SECONDS:
        transcript += " [transcript truncated: memo longer than supported window]"
    return transcript, len(offsets), duration


def _apply_derived_text_to_message(conn, message_id: str, derived_text: str) -> None:
    row = conn.execute(
        "SELECT m.chat_id, m.sender_name, c.name AS chat_name FROM messages m LEFT JOIN chats c ON c.chat_id = m.chat_id WHERE m.message_id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        return
    now = utc_now()
    conn.execute(
        "UPDATE messages SET text = ?, normalized_text = ?, updated_at = ? WHERE message_id = ?",
        (derived_text, normalize_text(derived_text), now, message_id),
    )
    conn.execute("DELETE FROM message_fts WHERE message_id = ?", (message_id,))
    conn.execute(
        "INSERT INTO message_fts(message_id, chat_id, chat_name, sender_name, text) VALUES (?, ?, ?, ?, ?)",
        (message_id, str(row["chat_id"]), str(row["chat_name"] or ""), str(row["sender_name"] or ""), derived_text),
    )


def reapply_derived_text(conn, message_id: str) -> bool:
    """Re-apply stored derived text after a sync upsert rewrote the message row."""
    row = conn.execute(
        "SELECT derived_text FROM attachment_derived_text WHERE message_id = ? AND status = 'done' AND derived_text != '' ORDER BY derived_id DESC LIMIT 1",
        (message_id,),
    ).fetchone()
    if row is None:
        return False
    _apply_derived_text_to_message(conn, message_id, str(row["derived_text"]))
    return True


def _store_derived(config: AppConfig, result: DerivedResult, attachment_id: str) -> None:
    now = utc_now()
    with open_db(config.archive.path) as conn:
        conn.execute(
            """
            INSERT INTO attachment_derived_text(
                message_id, chat_id, attachment_id, kind, status, derived_text,
                model_alias, chunk_count, duration_seconds, error_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, attachment_id) DO UPDATE SET
                status = excluded.status,
                derived_text = excluded.derived_text,
                model_alias = excluded.model_alias,
                chunk_count = excluded.chunk_count,
                duration_seconds = excluded.duration_seconds,
                error_text = excluded.error_text,
                updated_at = excluded.updated_at
            """,
            (
                result.message_id,
                result.chat_id,
                attachment_id,
                result.kind,
                result.status,
                result.derived_text,
                config.llm.model,
                result.chunk_count,
                result.duration_seconds,
                result.error_text,
                now,
                now,
            ),
        )
        if result.status == "done" and result.derived_text:
            _apply_derived_text_to_message(conn, result.message_id, result.derived_text)
        conn.commit()


def _primary_attachment(raw_json: str, kind: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    for att in payload.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        if att.get("isSticker") or att.get("isGif"):
            continue
        src = str(att.get("srcURL") or "")
        if not src:
            continue
        size = att.get("fileSize")
        if isinstance(size, (int, float)) and size > MAX_ATTACHMENT_BYTES:
            continue
        att_type = str(att.get("type") or "")
        if kind == "voice-memo" and (att_type == "audio" or att.get("isVoiceNote")):
            return att
        if kind == "image" and att_type == "img":
            return att
    return None


def derive_message_media(
    config: AppConfig,
    message_id: str,
    chat_id: str,
    raw_json: str,
    kind: str,
    llm_client: MediaLlmClient | None = None,
) -> DerivedResult:
    client = llm_client or OpenAiCompatMediaClient()
    attachment = _primary_attachment(raw_json, kind)
    if attachment is None:
        result = DerivedResult(message_id, chat_id, kind, "skipped", "", error_text="no usable attachment")
        _store_derived(config, result, attachment_id="(none)")
        return result

    attachment_id = str(attachment.get("id") or attachment.get("srcURL") or "")
    try:
        path = fetch_attachment(config, str(attachment["srcURL"]))
        if kind == "voice-memo":
            transcript, chunks, duration = transcribe_voice_memo(config, path, client)
            derived = f"{VOICE_PREFIX} {transcript}".strip()
            result = DerivedResult(message_id, chat_id, kind, "done", derived, chunk_count=chunks, duration_seconds=duration)
        else:
            description = client.describe_image(config, path.read_bytes(), str(attachment.get("mimeType") or ""))
            derived = f"{IMAGE_PREFIX} {description}".strip()
            result = DerivedResult(message_id, chat_id, kind, "done", derived)
    except MediaError as exc:
        result = DerivedResult(message_id, chat_id, kind, "failed", "", error_text=str(exc))

    _store_derived(config, result, attachment_id=attachment_id)
    trace_event("media.derived", {
        "message_id": message_id,
        "kind": kind,
        "status": result.status,
        "chunk_count": result.chunk_count,
        "duration_seconds": result.duration_seconds,
        "error": result.error_text,
    })
    return result


MEMO_NOUN_RE = re.compile(r"\b(?:voice\s+(?:memo|note|message)|memo)s?\b", re.IGNORECASE)
TRANSCRIPT_INTENT_RE = re.compile(r"\b(?:transcript|transcribe|transcription|verbatim|word\s+for\s+word|read\s+(?:it\s+)?(?:out|back))\b", re.IGNORECASE)
SUMMARY_INTENT_RE = re.compile(r"\b(?:summar|recap|tl;?dr|gist|main\s+points|overview)\w*\b", re.IGNORECASE)
DURATION_RE = re.compile(r"\b(\d{1,3})\s*(?:-|\s)?\s*(?:min|mins|minute|minutes)\b", re.IGNORECASE)
FROM_PERSON_RE = re.compile(r"\bfrom\s+([A-Za-zÀ-ÿ'. -]{2,40}?)(?:\s*(?:[?.!,]|$))", re.IGNORECASE)
LATEST_RE = re.compile(r"\b(?:last|latest|most\s+recent|newest)\b", re.IGNORECASE)


@dataclass(slots=True)
class MemoRequest:
    action: str  # "transcript" | "summary"
    mine_only: bool = False
    sender_query: str = ""
    duration_minutes: int | None = None


def parse_memo_request(question: str) -> MemoRequest | None:
    """Generic detection of voice-memo lookup requests. Command shapes only
    (memo noun plus a transcript/summary intent) — never question-literal."""
    if not MEMO_NOUN_RE.search(question):
        return None
    if TRANSCRIPT_INTENT_RE.search(question):
        action = "transcript"
    elif SUMMARY_INTENT_RE.search(question):
        action = "summary"
    else:
        return None
    duration_match = DURATION_RE.search(question)
    sender_match = FROM_PERSON_RE.search(question)
    mine_only = bool(re.search(r"\bmy\b", question, re.IGNORECASE)) and not sender_match
    return MemoRequest(
        action=action,
        mine_only=mine_only,
        sender_query=(sender_match.group(1).strip() if sender_match else ""),
        duration_minutes=(int(duration_match.group(1)) if duration_match else None),
    )


def find_voice_transcripts(
    config: AppConfig,
    *,
    mine_only: bool = False,
    sender_query: str = "",
    duration_minutes: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Newest-first stored voice transcripts, with optional sender and
    approximate-duration filters."""
    clauses = ["d.kind = 'voice-memo'", "d.status = 'done'", "d.derived_text != ''"]
    params: list[Any] = []
    if mine_only:
        clauses.append("m.is_sender = 1")
    if sender_query.strip():
        clauses.append("m.sender_name LIKE ?")
        params.append(f"%{sender_query.strip()}%")
    if duration_minutes is not None:
        tolerance = max(60.0, duration_minutes * 60 * 0.15)
        clauses.append("d.duration_seconds BETWEEN ? AND ?")
        params.extend([duration_minutes * 60 - tolerance, duration_minutes * 60 + tolerance])
    params.append(max(1, limit))
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            f"""
            SELECT d.message_id, d.derived_text, d.duration_seconds, d.chunk_count,
                   m.sender_name, m.timestamp, m.sort_key, c.name AS chat_name
            FROM attachment_derived_text d
            JOIN messages m ON m.message_id = d.message_id
            LEFT JOIN chats c ON c.chat_id = m.chat_id
            WHERE {' AND '.join(clauses)}
            ORDER BY m.sort_key DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    results = []
    for row in rows:
        text = str(row["derived_text"])
        if text.startswith(VOICE_PREFIX):
            text = text[len(VOICE_PREFIX):].strip()
        results.append(
            {
                "message_id": str(row["message_id"]),
                "transcript": text,
                "duration_seconds": float(row["duration_seconds"] or 0.0),
                "chunk_count": int(row["chunk_count"] or 0),
                "sender_name": str(row["sender_name"] or "unknown"),
                "timestamp": str(row["timestamp"] or ""),
                "chat_name": str(row["chat_name"] or ""),
            }
        )
    return results


def format_memo_header(memo: dict[str, Any]) -> str:
    minutes, seconds = divmod(int(memo["duration_seconds"]), 60)
    return (
        f"Voice memo from {memo['sender_name']} in {memo['chat_name']} "
        f"— {memo['timestamp'][:16].replace('T', ' ')} ({minutes}:{seconds:02d})"
    )


def summarize_transcript(config: AppConfig, memo: dict[str, Any], llm_client: Any | None = None) -> str:
    prompt = (
        f"Summarize this voice memo for the person catching up on it.\n"
        "Cover the main points and any requests, plans, times, or names mentioned.\n"
        "Keep it short. Do not invent content that is not in the transcript.\n"
        "Format for a plain-text chat app, not markdown: no #, *, or ** symbols; "
        "use '• ' for bullets and blank lines between points.\n"
        "Do not output chain-of-thought. Return only the summary.\n\n"
        f"{format_memo_header(memo)}\n\n"
        f"Transcript:\n{memo['transcript']}"
    )
    if llm_client is not None and hasattr(llm_client, "summarize_text"):
        return str(llm_client.summarize_text(config, prompt))
    from .llm import OpenAiCompatLlmClient

    client = llm_client if isinstance(llm_client, OpenAiCompatLlmClient) else OpenAiCompatLlmClient()
    return client._post_chat(
        config,
        [
            {"role": "system", "content": "Summarize transcripts faithfully. No hidden reasoning."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max(400, config.llm.max_output_tokens),
        trace_phase="memo.summary",
    )


KIND_MESSAGE_TYPES = {"voice-memo": ("VOICE",), "image": ("IMAGE",)}


def pending_media_messages(config: AppConfig, kind: str, limit: int, chat_query: str = "") -> list[dict[str, str]]:
    types = KIND_MESSAGE_TYPES[kind]
    placeholders = ",".join("?" for _ in types)
    params: list[Any] = list(types)
    chat_filter = ""
    if chat_query.strip():
        chat_filter = "AND m.chat_id IN (SELECT chat_id FROM chats WHERE name LIKE ?)"
        params.append(f"%{chat_query.strip()}%")
    excluded = [chat_id for chat_id in config.media.exclude_chat_ids if chat_id.strip()]
    if excluded:
        chat_filter += f" AND m.chat_id NOT IN ({','.join('?' for _ in excluded)})"
        params.extend(excluded)
    params.append(max(1, limit))
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            f"""
            SELECT m.message_id, m.chat_id, m.raw_json
            FROM messages m
            WHERE m.message_type IN ({placeholders})
              {chat_filter}
              AND NOT EXISTS (
                  SELECT 1 FROM attachment_derived_text d
                  WHERE d.message_id = m.message_id AND d.status IN ('done', 'skipped')
              )
            ORDER BY m.sort_key DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        {"message_id": str(row["message_id"]), "chat_id": str(row["chat_id"]), "raw_json": str(row["raw_json"])}
        for row in rows
    ]


def run_derivation_pass(
    config: AppConfig,
    kind: str,
    limit: int = 10,
    chat_query: str = "",
    llm_client: MediaLlmClient | None = None,
) -> list[DerivedResult]:
    candidates = pending_media_messages(config, kind, limit, chat_query)
    results: list[DerivedResult] = []
    for item in candidates:
        results.append(
            derive_message_media(
                config,
                item["message_id"],
                item["chat_id"],
                item["raw_json"],
                kind,
                llm_client,
            )
        )
    return results
