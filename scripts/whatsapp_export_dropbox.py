#!/usr/bin/env python3
"""Private drop-folder wrapper around beeper-bot's reviewed WhatsApp importer."""
from __future__ import annotations

import argparse
import csv
import errno
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import stat
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from beeper_bot.config import DEFAULT_CONFIG_PATH, load_config
from beeper_bot.db import open_db
from beeper_bot.offline_archive import import_whatsapp

METADATA = "chat.json"
RESERVED = {"Processed", "Failed"}
DEFAULT_MANIFEST_PATH = Path.home() / ".config" / "beeper-bot" / "whatsapp-export-chats.tsv"
QUIET_SECONDS = 30


def _private(path: Path, kind: str) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{kind} must not be a symlink")
    expected = stat.S_ISDIR if kind in {"root", "folder"} else stat.S_ISREG
    if not expected(info.st_mode) or info.st_mode & 0o077:
        raise ValueError(f"{kind} must be private and the expected file type")


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_json(path: Path, value: dict[str, object]) -> None:
    _mkdir(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _inside(path: Path, root: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("path escapes drop root")


def _destination(root: Path, kind: str, folder: str, digest: str) -> Path:
    current = root / kind
    for part in (current, current / folder, current / folder / digest):
        if part.exists() or part.is_symlink():
            _inside(part, root)
            _private(part, "folder")
        else:
            _mkdir(part)
    return current / folder / digest


def _metadata(folder: Path) -> tuple[str, str]:
    path = folder / METADATA
    if not path.exists():
        raise ValueError("missing chat.json")
    _private(path, "metadata")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"chat_id", "name"}:
        raise ValueError("chat.json must contain only chat_id and name")
    chat_id, name = raw["chat_id"], raw["name"]
    if not isinstance(chat_id, str) or not chat_id.strip() or not isinstance(name, str) or not name.strip():
        raise ValueError("chat.json values must be non-empty strings")
    return chat_id.strip(), name.strip()


def _message_count(db: Path, chat_id: str) -> int:
    if not db.exists():
        return 0
    with open_db(db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0])


@contextmanager
def _lock(path: Path):
    _mkdir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("scanner is already running") from None
        yield


def _read_manifest(manifest: Path) -> dict[str, tuple[str, str]]:
    manifest = manifest.expanduser()
    _private(manifest, "manifest")
    with manifest.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        if rows.fieldnames != ["folder", "chat_id", "name"]:
            raise ValueError("manifest header must be: folder<TAB>chat_id<TAB>name")
        raw_entries = list(rows)
    entries: dict[str, tuple[str, str]] = {}
    seen_ids: set[str] = set()
    for row in raw_entries:
        folder_name, chat_id, name = (row[key].strip() for key in ("folder", "chat_id", "name"))
        if (not folder_name or folder_name in RESERVED or folder_name in {".", ".."} or "/" in folder_name
                or "\0" in folder_name or not chat_id or not name):
            raise ValueError("manifest contains an invalid value")
        if chat_id in seen_ids or folder_name in entries:
            raise ValueError("manifest contains a duplicate folder or chat_id")
        seen_ids.add(chat_id)
        entries[folder_name] = (chat_id, name)
    return entries


def _move(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(source, destination)


def scan(root: Path, config_path: Path, manifest: Path = DEFAULT_MANIFEST_PATH) -> dict[str, object]:
    root = root.expanduser()
    _private(root, "root")
    config = load_config(config_path.expanduser())
    expected_folders = _read_manifest(manifest)
    lock_path = config.archive.path.parent / "whatsapp-export-dropbox.lock"
    folder_outcomes: dict[str, dict[str, object]] = {}
    result: dict[str, object] = {"imported": 0, "duplicates": 0, "failed": 0, "files": 0,
                                 "media_extracted": 0, "media_skipped_video": 0, "media_failed": 0,
                                 "folders": folder_outcomes}
    with _lock(lock_path):
        ids: set[str] = set()
        for folder in sorted(root.iterdir(), key=lambda item: item.name):
            if folder.name in RESERVED:
                _private(folder, "folder")
                continue
            outcome: dict[str, object] = {"imported": 0, "duplicates": 0, "failed": 0, "files": 0, "skipped": 0,
                                          "media_extracted": 0, "media_skipped_video": 0, "media_failed": 0}
            folder_outcomes[folder.name] = outcome
            try:
                _inside(folder, root)
                _private(folder, "folder")
                chat_id, name = _metadata(folder)
                if expected_folders.get(folder.name) != (chat_id, name):
                    raise ValueError("folder metadata does not match manifest")
                if chat_id in ids:
                    raise ValueError("duplicate chat_id in drop root")
                ids.add(chat_id)
                sources: list[Path] = []
                for entry in sorted(folder.iterdir(), key=lambda item: item.name):
                    _inside(entry, root)
                    if entry.name == METADATA:
                        continue
                    if entry.is_symlink():
                        raise ValueError("source must not be a symlink")
                    if entry.suffix.lower() not in {".zip", ".txt"}:
                        outcome["skipped"] += 1
                        continue
                    _private(entry, "source")
                    if time.time() - entry.stat().st_mtime < QUIET_SECONDS:
                        outcome["skipped"] += 1
                        continue
                    sources.append(entry)
                for source in sources:
                    with source.open("rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    before = _message_count(config.archive.path, chat_id)
                    receipt: dict[str, object] = {
                        "filename": source.name, "sha256": digest, "chat_id": chat_id,
                        "chat_name": name, "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    destination_kind = "Processed"
                    try:
                        payload = import_whatsapp(config, source, chat_id, name, "day-first", extract_media=True,
                                                  forbidden_media_root=root)
                        after = _message_count(config.archive.path, chat_id)
                        imported = max(0, after - before)
                        duplicates = max(0, int(payload["message_count"]) - imported)
                        media_counts = {key: int(payload[key]) for key in
                                        ("media_extracted", "media_skipped_video", "media_failed")}
                        receipt.update(imported_count=imported, duplicate_count=duplicates, **media_counts)
                        result["imported"] += imported
                        result["duplicates"] += duplicates
                        outcome["imported"] += imported
                        outcome["duplicates"] += duplicates
                        for key, count in media_counts.items():
                            result[key] += count
                            outcome[key] += count
                    except Exception as exc:
                        destination_kind = "Failed"
                        receipt.update(imported_count=0, duplicate_count=0, media_extracted=0,
                                       media_skipped_video=0, media_failed=0,
                                       error=f"import failed ({type(exc).__name__})")
                        result["failed"] += 1
                        outcome["failed"] += 1
                    destination = _destination(root, destination_kind, folder.name, digest)
                    _move(source, destination / source.name)
                    _write_json(destination / "receipt.json", receipt)
                    result["files"] += 1
                    outcome["files"] += 1
            except Exception as exc:
                outcome["error"] = f"folder refused ({type(exc).__name__})"
    return result


def setup(root: Path, manifest: Path) -> int:
    root = root.expanduser()
    if root.exists():
        _private(root, "root")
    else:
        _mkdir(root)
    entries = _read_manifest(manifest)
    for folder_name, (chat_id, name) in entries.items():
        folder = root / folder_name
        if folder.exists():
            _private(folder, "folder")
            if _metadata(folder) != (chat_id, name):
                raise ValueError("existing folder metadata does not match manifest")
        else:
            _mkdir(folder)
            _write_json(folder / METADATA, {"chat_id": chat_id, "name": name})
    for name in RESERVED:
        _mkdir(root / name)
    return len(entries)


def install_launchd(root: Path, config_path: Path, manifest: Path, script: Path) -> Path:
    state = Path.home() / ".local" / "state" / "beeper-bot"
    _mkdir(state)
    log = state / "whatsapp-export-dropbox.log"
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(log, 0o600)
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / "com.exceptionalspirits.whatsapp-export-dropbox.plist"
    payload = {
        "Label": "com.exceptionalspirits.whatsapp-export-dropbox",
        "ProgramArguments": [sys.executable, str(script.resolve()), "scan", "--root", str(root.expanduser()),
                             "--config", str(config_path.expanduser()), "--manifest", str(manifest.expanduser())],
        "StartInterval": 60, "RunAtLoad": True,
        "StandardOutPath": str(log), "StandardErrorPath": str(log), "ProcessType": "Background",
    }
    temporary = plist.with_name(f".{plist.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle)
    os.chmod(temporary, 0o600)
    os.replace(temporary, plist)
    return plist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--root", type=Path, default=Path.home() / "WhatsApp Exports")
    scan_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    scan_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    setup_parser = sub.add_parser("setup")
    setup_parser.add_argument("manifest", type=Path)
    setup_parser.add_argument("--root", type=Path, default=Path.home() / "WhatsApp Exports")
    install = sub.add_parser("install-launchd")
    install.add_argument("--root", type=Path, default=Path.home() / "WhatsApp Exports")
    install.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    install.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            print(json.dumps(scan(args.root, args.config, args.manifest), sort_keys=True))
        elif args.command == "setup":
            print(f"created or verified {setup(args.root, args.manifest)} chat folders")
        else:
            print(install_launchd(args.root, args.config, args.manifest, Path(__file__)))
        return 0
    except Exception as exc:
        print(f"whatsapp export dropbox: {type(exc).__name__}: operation refused", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
