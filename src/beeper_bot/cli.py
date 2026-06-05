from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .beeper_api import BeeperApiClient, BeeperApiError
from .bridge import ControlBridge
from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from .db import SCHEMA_VERSION, collect_runtime_status, init_db_path
from .llm import LlmError, ask_archive, format_ask_response
from .retrieval import format_find_response, search_archive
from .sync import sync_chats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-first Beeper memory bot")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config TOML file (default: {DEFAULT_CONFIG_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Create the SQLite archive schema")
    init_db.add_argument("--json", action="store_true", help="Print machine-readable output")

    status = subparsers.add_parser("status", help="Show archive and runtime status")
    status.add_argument("--json", action="store_true", help="Print machine-readable output")

    sync = subparsers.add_parser("sync", help="Sync allowlisted chats from the Beeper local API")
    sync.add_argument("--json", action="store_true", help="Print machine-readable output")
    sync.add_argument("--chat-id", action="append", dest="chat_ids", help="Sync one specific chat ID; repeatable")

    find = subparsers.add_parser("find", help="Search the local archive")
    find.add_argument("query", nargs="+", help="Search query")
    find.add_argument("--json", action="store_true", help="Print machine-readable output")
    find.add_argument("--limit", type=int, default=5, help="Maximum number of results (default: 5)")

    ask = subparsers.add_parser("ask", help="Search the archive and answer from evidence")
    ask.add_argument("question", nargs="+", help="Question to answer")
    ask.add_argument("--json", action="store_true", help="Print machine-readable output")
    ask.add_argument("--limit", type=int, default=None, help="Maximum number of evidence snippets")

    serve = subparsers.add_parser("serve", help="Poll the private control chat")
    serve.add_argument("--once", action="store_true", help="Run one poll pass and exit")
    serve.add_argument("--json", action="store_true", help="Print machine-readable output for --once")

    return parser


def _status_payload(config_path: Path):
    config = load_config(config_path)
    status = collect_runtime_status(config)
    return {
        "config_path": str(status.config_path),
        "control_chat_configured": status.control_chat_configured,
        "indexed_chat_count": status.indexed_chat_count,
        "beeper_api_base": status.beeper_api_base,
        "llm_base_url": status.llm_base_url,
        "database": {
            "path": str(status.database.path),
            "file_exists": status.database.file_exists,
            "file_size_bytes": status.database.file_size_bytes,
            "schema_version": status.database.schema_version,
            "expected_schema_version": SCHEMA_VERSION,
            "chat_count": status.database.chat_count,
            "message_count": status.database.message_count,
            "fts_count": status.database.fts_count,
            "sync_state_count": status.database.sync_state_count,
            "runtime_state_count": status.database.runtime_state_count,
        },
    }


def cmd_init_db(config_path: Path, as_json: bool) -> int:
    config = load_config(config_path)
    init_db_path(config.archive.path)
    payload = {
        "ok": True,
        "config_path": str(config.config_path),
        "database_path": str(config.archive.path),
        "schema_version": SCHEMA_VERSION,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Initialized database: {config.archive.path}")
        print(f"Schema version: {SCHEMA_VERSION}")
    return 0


def cmd_status(config_path: Path, as_json: bool) -> int:
    payload = _status_payload(config_path)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Config: {payload['config_path']}")
    print(f"Control chat configured: {'yes' if payload['control_chat_configured'] else 'no'}")
    print(f"Indexed chats: {payload['indexed_chat_count']}")
    print(f"Beeper API: {payload['beeper_api_base']}")
    print(f"LLM API: {payload['llm_base_url']}")
    print(f"Database: {payload['database']['path']}")
    print(f"Database exists: {'yes' if payload['database']['file_exists'] else 'no'}")
    print(f"Database size: {payload['database']['file_size_bytes']} bytes")
    print(
        "Schema version: "
        f"{payload['database']['schema_version']} "
        f"(expected {payload['database']['expected_schema_version']})"
    )
    print(f"Chats: {payload['database']['chat_count']}")
    print(f"Messages: {payload['database']['message_count']}")
    print(f"FTS rows: {payload['database']['fts_count']}")
    print(f"Sync state rows: {payload['database']['sync_state_count']}")
    print(f"Runtime state rows: {payload['database']['runtime_state_count']}")
    return 0


def cmd_sync(config_path: Path, chat_ids: list[str] | None, as_json: bool) -> int:
    config = load_config(config_path)
    target_chat_ids = chat_ids or list(config.beeper.indexed_chat_ids)
    if not target_chat_ids:
        raise ConfigError("No indexed chats configured and no --chat-id values supplied")

    client = BeeperApiClient(config.beeper)
    result = sync_chats(config, client, target_chat_ids)
    payload = {
        "ok": True,
        "chat_count": len(result.chats),
        "total_fetched_messages": result.total_fetched_messages,
        "total_stored_messages": result.total_stored_messages,
        "chats": [
            {
                "chat_id": chat.chat_id,
                "chat_name": chat.chat_name,
                "fetched_messages": chat.fetched_messages,
                "stored_messages": chat.stored_messages,
                "latest_sort_key": chat.latest_sort_key,
            }
            for chat in result.chats
        ],
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Synced chats: {payload['chat_count']}")
        print(f"Fetched messages: {payload['total_fetched_messages']}")
        print(f"Stored messages: {payload['total_stored_messages']}")
        for chat in result.chats:
            print(
                f"- {chat.chat_name} ({chat.chat_id}): "
                f"fetched={chat.fetched_messages} stored={chat.stored_messages} latest_sort_key={chat.latest_sort_key}"
            )
    return 0


def cmd_find(config_path: Path, query_parts: list[str], limit: int, as_json: bool) -> int:
    config = load_config(config_path)
    response = search_archive(config, " ".join(query_parts), limit=limit)
    if as_json:
        payload = {
            "query": response.query,
            "result_count": len(response.results),
            "results": [
                {
                    "message_id": result.message_id,
                    "chat_id": result.chat_id,
                    "chat_name": result.chat_name,
                    "sender_name": result.sender_name,
                    "timestamp": result.timestamp,
                    "text": result.text,
                    "score": result.score,
                    "match_reasons": result.match_reasons,
                }
                for result in response.results
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_find_response(response))
    return 0


def cmd_ask(config_path: Path, question_parts: list[str], limit: int | None, as_json: bool) -> int:
    config = load_config(config_path)
    response = ask_archive(config, " ".join(question_parts), limit=limit)
    if as_json:
        payload = {
            "question": response.question,
            "answer": response.answer,
            "evidence_count": len(response.evidence),
            "evidence": [
                {
                    "citation_id": item.citation_id,
                    "message_id": item.message_id,
                    "chat_id": item.chat_id,
                    "chat_name": item.chat_name,
                    "sender_name": item.sender_name,
                    "timestamp": item.timestamp,
                    "excerpt": item.excerpt,
                    "score": item.score,
                }
                for item in response.evidence
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_ask_response(response))
    return 0


def cmd_serve(config_path: Path, once: bool, as_json: bool) -> int:
    config = load_config(config_path)
    bridge = ControlBridge(config)
    if once:
        result = bridge.process_once()
        payload = {
            "processed_messages": result.processed_messages,
            "replied_messages": result.replied_messages,
            "busy_messages": result.busy_messages,
        }
        if as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Processed messages: {result.processed_messages}")
            print(f"Replied messages: {result.replied_messages}")
            print(f"Busy messages: {result.busy_messages}")
        return 0

    if as_json:
        raise ConfigError("--json is only supported with serve --once")
    bridge.serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser()

    try:
        if args.command == "init-db":
            return cmd_init_db(config_path, args.json)
        if args.command == "status":
            return cmd_status(config_path, args.json)
        if args.command == "sync":
            return cmd_sync(config_path, args.chat_ids, args.json)
        if args.command == "find":
            return cmd_find(config_path, args.query, args.limit, args.json)
        if args.command == "ask":
            return cmd_ask(config_path, args.question, args.limit, args.json)
        if args.command == "serve":
            return cmd_serve(config_path, args.once, args.json)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except BeeperApiError as exc:
        print(f"Beeper API error: {exc}", file=sys.stderr)
        return 1
    except LlmError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"OS error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unhandled command: {args.command}")
    return 2
