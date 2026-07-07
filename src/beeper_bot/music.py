"""The music-library chat: a Claude tool loop over music-library-project scripts.

Free text in the "music" control chat becomes a conversation with a cloud model
(purpose "music" must be opted into [cloud_llm].purposes) that can inspect the
library through read-only tools and file work into the fixer queue. It NEVER
writes to the library itself — the queue is drained by a separate, gated agent
(music-library-project's fixer drain), and resolutions ping this chat via
`beeper-bot notify`.

Every tool is a subprocess over the music-library-project scripts on this host
(venus): argv lists only, per-tool timeouts, output truncated before it goes
back to the model. Tools that need the beets DB or master audio run inside the
`music-beets` container via docker exec.
"""
from __future__ import annotations

import json
import re
import subprocess

from .config import AppConfig
from .llm import anthropic_messages

TOOL_OUTPUT_MAX_CHARS = 6000
ISSUE_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")

MUSIC_TOOLS: list[dict] = [
    {
        "name": "now_playing",
        "description": "What is playing right now on Navidrome (artist, title, album, who is listening, minutes ago). Call this whenever the user says 'this track/song' or asks about current playback.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "queue_list",
        "description": "List fixer-queue issues (newest 20). Use status 'new' for open work, 'done' for recent resolutions, 'all' for both.",
        "input_schema": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["new", "done", "all"]}},
            "additionalProperties": False,
        },
    },
    {
        "name": "capture_issue",
        "description": "File a library issue or request into the fixer queue, snapshotting what's playing right now. This is THE way to request any library change (upgrade quality, fix tags/art, acquire music) — a separate gated agent drains the queue and this chat gets pinged when it lands. Write the text as a clear instruction for that agent.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The issue/request, self-contained enough for the fixer agent to act on."}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_issue",
        "description": "Mark a fixer-queue issue done with a note. Use it (a) to record the user's answer to a pending fixer question (the note IS the answer, verbatim), or (b) when the user says an issue is moot/already handled.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Issue id, e.g. 20260707-121458-4fbc"},
                "note": {"type": "string", "description": "Resolution text or the user's answer."},
            },
            "required": ["id", "note"],
            "additionalProperties": False,
        },
    },
    {
        "name": "diagnose_track",
        "description": "Deep-inspect one track: beets metadata, provenance tags, embedded art, spectral quality analysis (real frequency content — the authoritative quality verdict; bitrate lies), quarantine/ledger state. Takes a master path (/mnt/music-synology/...) or a beets id. Slow (~up to 2 min). Use for 'why does this sound bad' questions.",
        "input_schema": {
            "type": "object",
            "properties": {"path_or_beets_id": {"type": "string"}},
            "required": ["path_or_beets_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "navidrome_search",
        "description": "Search the streaming library (Navidrome) for artists/albums/songs by name.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "beets_lookup",
        "description": "Query the beets master database. Takes a beets query string (e.g. 'artist:Kraftwerk', 'album:\"Tour de France\"', a bare title). Returns id|artist|title|album|format|bitrate lines (max 50).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

SYSTEM_PROMPT = """You are the music-library assistant in the owner's private "beeper-beets" chat. \
You know his self-hosted pipeline: Navidrome streams the master library (a beets-managed tree on \
/mnt/music-synology), and a fixer queue + gated acquisition pipeline (slskd/Lidarr, spectral quality \
gates) handles all changes.

Ground rules:
- Identity ≠ quality: spectral analysis proves quality, not that it's the right song. Bitrate tags lie; \
only diagnose_track's spectral verdict counts. Some tracks are vintage/lossy masters — nothing better \
exists, and re-grabbing won't help (check done queue entries for prior verdicts before re-filing).
- You NEVER modify the library directly. Any change the user wants (upgrade, retag, art fix, new music) \
becomes a capture_issue entry; a separate gated agent drains the queue and this chat is pinged when \
things land. Tell the user you've filed it, don't promise you did the work.
- Pending fixer questions (queue entries from source drain-question) are questions TO the user. When \
the user answers one, record it with resolve_issue(question_id, their answer).
- "this track/song" = call now_playing first; the snapshot is the whole trick.
- Keep replies chat-sized and conversational. Lead with the answer. No markdown tables.
"""


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) > TOOL_OUTPUT_MAX_CHARS:
        return text[:TOOL_OUTPUT_MAX_CHARS] + "\n...[truncated]"
    return text


def _run(argv: list[str], timeout: int) -> tuple[str, bool]:
    """Run a tool subprocess; returns (output, is_error). Never raises."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"tool timed out after {timeout}s", True
    except Exception as exc:
        return f"tool failed: {exc.__class__.__name__}: {exc}", True
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0:
        return _truncate(out or f"exited rc={proc.returncode}"), True
    return _truncate(out or "(no output)"), False


def read_issues(config: AppConfig) -> list[dict]:
    """Defensive read of the fixer queue (skips torn/garbage lines)."""
    path = config.music.project_root / "state" / "fixer_queue" / "issues.jsonl"
    items: list[dict] = []
    try:
        raw = path.read_text()
    except OSError:
        return items
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id"):
            items.append(item)
    return items


def run_music_tool(config: AppConfig, name: str, args: dict) -> tuple[str, bool]:
    music = config.music
    scripts = str(music.project_root / "scripts")
    if name == "now_playing":
        return _run([music.host_python, f"{scripts}/navidrome_api.py", "getNowPlaying"], timeout=15)
    if name == "queue_list":
        status = str(args.get("status") or "new")
        if status not in ("new", "done", "all"):
            return f"invalid status {status!r}", True
        items = [i for i in read_issues(config) if status == "all" or i.get("status") == status]
        return _truncate(json.dumps(items[-20:], indent=1, ensure_ascii=False)), False
    if name == "capture_issue":
        text = str(args.get("text") or "").strip()
        if not text:
            return "capture_issue needs non-empty text", True
        return _run([music.host_python, f"{scripts}/fixer_capture.py", text], timeout=45)
    if name == "resolve_issue":
        issue_id = str(args.get("id") or "").strip()
        note = str(args.get("note") or "").strip()
        if not ISSUE_ID_RE.match(issue_id):
            return f"invalid issue id {issue_id!r}", True
        if not note:
            return "resolve_issue needs a note", True
        return _run([music.host_python, f"{scripts}/fixer_capture.py", "--resolve", issue_id, note], timeout=15)
    if name == "diagnose_track":
        target = str(args.get("path_or_beets_id") or "").strip()
        if not target:
            return "diagnose_track needs a path or beets id", True
        return _run(
            ["docker", "exec", music.docker_container, "python3", "scripts/diagnose_track.py", target],
            timeout=120,
        )
    if name == "navidrome_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return "navidrome_search needs a query", True
        return _run([music.host_python, f"{scripts}/navidrome_api.py", "search", query], timeout=15)
    if name == "beets_lookup":
        query = str(args.get("query") or "").strip()
        if not query:
            return "beets_lookup needs a query", True
        out, is_err = _run(
            ["docker", "exec", music.docker_container, "beet", "ls",
             "-f", "$id|$artist|$title|$album|$format|$bitrate", query],
            timeout=30,
        )
        if not is_err:
            lines = out.splitlines()
            if len(lines) > 50:
                out = "\n".join(lines[:50]) + f"\n...[{len(lines) - 50} more]"
        return out, is_err
    return f"unknown tool {name!r}", True


def _context_block(config: AppConfig) -> str:
    """Live context prepended to the system prompt: now playing + queue counts."""
    parts = []
    np, np_err = _run(
        [config.music.host_python, str(config.music.project_root / "scripts" / "navidrome_api.py"), "getNowPlaying"],
        timeout=15,
    )
    parts.append("Now playing (raw): " + ("(unavailable)" if np_err else np[:1200]))
    issues = read_issues(config)
    new = [i for i in issues if i.get("status") == "new"]
    questions = [i for i in new if i.get("source") == "drain-question"]
    parts.append(f"Fixer queue: {len(new) - len(questions)} open, {len(questions)} pending questions for the user.")
    for q in questions[-3:]:
        parts.append(f"Pending question [{q['id']}]: {q.get('text', '')}")
    return "\n".join(parts)


def music_chat_turn(config: AppConfig, user_text: str, turns: list[dict[str, str]] | None = None) -> str:
    """One conversational turn: Claude + tools until it stops asking for tools."""
    system = SYSTEM_PROMPT + "\n\nLive context:\n" + _context_block(config)
    messages: list[dict] = []
    for turn in turns or []:
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            if not messages and role == "assistant":
                continue  # first message must be a user turn
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})

    for _ in range(max(1, config.music.max_tool_iterations)):
        response = anthropic_messages(
            config,
            messages,
            system=system,
            tools=MUSIC_TOOLS,
            max_tokens=config.music.max_output_tokens,
            purpose="music",
        )
        content = response.get("content") or []
        if response.get("stop_reason") == "tool_use":
            # Echo the assistant content verbatim (thinking blocks included).
            messages.append({"role": "assistant", "content": content})
            results = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                output, is_error = run_music_tool(config, str(block.get("name") or ""), block.get("input") or {})
                result: dict = {"type": "tool_result", "tool_use_id": block.get("id"), "content": output}
                if is_error:
                    result["is_error"] = True
                results.append(result)
            messages.append({"role": "user", "content": results})
            continue
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        reply = "\n".join(t for t in texts if t).strip()
        return reply or "(the music brain returned nothing — try rephrasing)"
    return "I ran out of tool budget before finishing — ask again and I'll pick it up from the queue."
