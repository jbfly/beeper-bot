from __future__ import annotations

import json
import subprocess
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .config import AppConfig
from .db import open_db, utc_now
from .memory import load_memory_state, recent_control_turns
from .people import load_person_graph


_current_trace_id: ContextVar[str | None] = ContextVar("current_trace_id", default=None)
_current_trace_db_path: ContextVar[Path | None] = ContextVar("current_trace_db_path", default=None)
_current_trace_seq: ContextVar[int] = ContextVar("current_trace_seq", default=0)


@dataclass(slots=True)
class TraceHandle:
    trace_id: str
    db_path: Path


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def start_trace(config: AppConfig, kind: str, *, question: str = "", source: str = "console") -> TraceHandle:
    trace_id = uuid.uuid4().hex
    now = utc_now()
    with open_db(config.archive.path) as conn:
        conn.execute(
            """
            INSERT INTO traces(trace_id, trace_kind, source, question, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (trace_id, kind.strip() or "ask", source.strip() or "console", question.strip(), now, now),
        )
        conn.commit()
    return TraceHandle(trace_id=trace_id, db_path=config.archive.path)


@contextmanager
def trace_context(config: AppConfig, kind: str, *, question: str = "", source: str = "console") -> Iterator[TraceHandle]:
    handle = start_trace(config, kind, question=question, source=source)
    tok_id = _current_trace_id.set(handle.trace_id)
    tok_path = _current_trace_db_path.set(handle.db_path)
    tok_seq = _current_trace_seq.set(0)
    try:
        yield handle
    except Exception as exc:
        finish_trace(handle, status="error", error_text=str(exc))
        raise
    else:
        finish_trace(handle, status="ok")
    finally:
        _current_trace_id.reset(tok_id)
        _current_trace_db_path.reset(tok_path)
        _current_trace_seq.reset(tok_seq)


def current_trace_id() -> str | None:
    return _current_trace_id.get()


def trace_event(event_kind: str, payload: Any) -> None:
    trace_id = _current_trace_id.get()
    db_path = _current_trace_db_path.get()
    if not trace_id or db_path is None:
        return
    seq = _current_trace_seq.get() + 1
    _current_trace_seq.set(seq)
    now = utc_now()
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trace_events(trace_id, seq_no, event_kind, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (trace_id, seq, event_kind, json.dumps(_jsonable(payload), sort_keys=True), now),
        )
        conn.execute(
            "UPDATE traces SET updated_at = ? WHERE trace_id = ?",
            (now, trace_id),
        )
        conn.commit()


def finish_trace(handle: TraceHandle, *, status: str = "ok", final_answer: str = "", error_text: str = "") -> None:
    now = utc_now()
    with open_db(handle.db_path) as conn:
        conn.execute(
            """
            UPDATE traces
            SET status = CASE WHEN status = 'running' OR ? != '' THEN ? ELSE status END,
                final_answer = CASE WHEN ? != '' THEN ? ELSE final_answer END,
                error_text = CASE WHEN ? != '' THEN ? ELSE error_text END,
                finished_at = COALESCE(finished_at, ?),
                updated_at = ?
            WHERE trace_id = ?
            """,
            (
                status,
                status,
                final_answer.strip(),
                final_answer.strip(),
                error_text.strip(),
                error_text.strip(),
                now,
                now,
                handle.trace_id,
            ),
        )
        conn.commit()


def telemetry_sample() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
        line = next((item.strip() for item in proc.stdout.splitlines() if item.strip()), "")
        util, mem_used, mem_total, temp = [part.strip() for part in line.split(",", 3)]
        return {
            "created_at": utc_now(),
            "gpu_util": float(util),
            "vram_used_mb": float(mem_used),
            "vram_total_mb": float(mem_total),
            "gpu_temp_c": float(temp),
        }
    except Exception as exc:
        return {
            "created_at": utc_now(),
            "gpu_util": None,
            "vram_used_mb": None,
            "vram_total_mb": None,
            "gpu_temp_c": None,
            "error": str(exc),
        }


def record_telemetry(config: AppConfig, sample: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = sample or telemetry_sample()
    with open_db(config.archive.path) as conn:
        conn.execute(
            """
            INSERT INTO telemetry_samples(created_at, gpu_util, vram_used_mb, vram_total_mb, gpu_temp_c, error_text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload.get("created_at") or utc_now()),
                payload.get("gpu_util"),
                payload.get("vram_used_mb"),
                payload.get("vram_total_mb"),
                payload.get("gpu_temp_c"),
                str(payload.get("error") or ""),
            ),
        )
        conn.execute(
            "DELETE FROM telemetry_samples WHERE sample_id NOT IN (SELECT sample_id FROM telemetry_samples ORDER BY sample_id DESC LIMIT 7200)"
        )
        conn.commit()
    return payload


def list_telemetry(config: AppConfig, limit: int = 180) -> list[dict[str, Any]]:
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            """
            SELECT created_at, gpu_util, vram_used_mb, vram_total_mb, gpu_temp_c, error_text
            FROM telemetry_samples
            ORDER BY sample_id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    items = []
    for row in reversed(rows):
        items.append(
            {
                "created_at": str(row["created_at"]),
                "gpu_util": row["gpu_util"],
                "vram_used_mb": row["vram_used_mb"],
                "vram_total_mb": row["vram_total_mb"],
                "gpu_temp_c": row["gpu_temp_c"],
                "error": str(row["error_text"] or ""),
            }
        )
    return items


def list_traces(config: AppConfig, limit: int = 40) -> list[dict[str, Any]]:
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            """
            SELECT trace_id, trace_kind, source, question, status, created_at, updated_at, finished_at, final_answer, error_text
            FROM traces
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [
        {
            "trace_id": str(row["trace_id"]),
            "trace_kind": str(row["trace_kind"]),
            "source": str(row["source"]),
            "question": str(row["question"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "finished_at": str(row["finished_at"] or ""),
            "final_answer": str(row["final_answer"] or ""),
            "error_text": str(row["error_text"] or ""),
        }
        for row in rows
    ]


def get_trace(config: AppConfig, trace_id: str) -> dict[str, Any] | None:
    with open_db(config.archive.path) as conn:
        header = conn.execute(
            """
            SELECT trace_id, trace_kind, source, question, status, created_at, updated_at, finished_at, final_answer, error_text
            FROM traces WHERE trace_id = ?
            """,
            (trace_id,),
        ).fetchone()
        if header is None:
            return None
        events = conn.execute(
            """
            SELECT seq_no, event_kind, payload_json, created_at
            FROM trace_events
            WHERE trace_id = ?
            ORDER BY seq_no ASC, event_id ASC
            """,
            (trace_id,),
        ).fetchall()
    return {
        "trace_id": str(header["trace_id"]),
        "trace_kind": str(header["trace_kind"]),
        "source": str(header["source"]),
        "question": str(header["question"]),
        "status": str(header["status"]),
        "created_at": str(header["created_at"]),
        "updated_at": str(header["updated_at"]),
        "finished_at": str(header["finished_at"] or ""),
        "final_answer": str(header["final_answer"] or ""),
        "error_text": str(header["error_text"] or ""),
        "events": [
            {
                "seq_no": int(row["seq_no"]),
                "event_kind": str(row["event_kind"]),
                "created_at": str(row["created_at"]),
                "payload": json.loads(str(row["payload_json"] or "{}")),
            }
            for row in events
        ],
    }


def snapshot_memory(config: AppConfig) -> dict[str, Any]:
    graph = load_person_graph(config)
    return {
        "control_turns": recent_control_turns(config, limit=12),
        "memory_state": load_memory_state(config),
        "people": [
            {
                "person_id": person.person_id,
                "canonical_name": person.canonical_name,
                "aliases": list(person.aliases),
                "chat_ids": list(person.chat_ids),
            }
            for person in graph.people
        ],
        "pending_updates": _pending_updates(config),
    }


def _pending_updates(config: AppConfig) -> list[dict[str, Any]]:
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            """
            SELECT update_id, update_kind, payload_json, status, created_at, updated_at
            FROM memory_updates
            ORDER BY update_id DESC
            LIMIT 20
            """
        ).fetchall()
    items = []
    for row in rows:
        items.append(
            {
                "update_id": int(row["update_id"]),
                "update_kind": str(row["update_kind"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "payload": json.loads(str(row["payload_json"])),
            }
        )
    return items
