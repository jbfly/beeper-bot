#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "alpha-matrix-wake" / "config.toml"
DEFAULT_STATE_PATH = Path.home() / ".local" / "state" / "alpha-matrix-wake" / "state.json"


@dataclass(slots=True)
class Config:
    homeserver: str
    access_token_file: Path
    user_id: str
    room_id: str
    reply_prefix: str
    alpha_host: str
    alpha_mac: str
    alpha_ssh_user: str
    alpha_ssh_key: Path
    alpha_awake_lease: str
    sync_timeout_ms: int
    wake_timeout_seconds: int
    trigger_cooldown_seconds: int
    state_path: Path


def log(msg: str) -> None:
    print(f"alpha-matrix-wake: {msg}", flush=True)


def die(msg: str) -> "NoReturn":
    raise SystemExit(msg)


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    matrix = raw.get("matrix", {})
    alpha = raw.get("alpha", {})
    runtime = raw.get("runtime", {})
    state = raw.get("state", {})
    return Config(
        homeserver=str(matrix["homeserver"]).rstrip("/"),
        access_token_file=Path(os.path.expanduser(str(matrix["access_token_file"]))),
        user_id=str(matrix["user_id"]),
        room_id=str(matrix["room_id"]),
        reply_prefix=str(matrix.get("reply_prefix", "[BEEPER-BOT] ")),
        alpha_host=str(alpha["host"]),
        alpha_mac=str(alpha["mac"]),
        alpha_ssh_user=str(alpha.get("ssh_user", os.environ.get("USER", "jbfly"))),
        alpha_ssh_key=Path(os.path.expanduser(str(alpha["ssh_key"]))),
        alpha_awake_lease=str(alpha.get("awake_lease", "2h")),
        sync_timeout_ms=int(runtime.get("sync_timeout_ms", 30000)),
        wake_timeout_seconds=int(runtime.get("wake_timeout_seconds", 180)),
        trigger_cooldown_seconds=int(runtime.get("trigger_cooldown_seconds", 60)),
        state_path=Path(os.path.expanduser(str(state.get("path", DEFAULT_STATE_PATH)))),
    )


def load_access_token(path: Path) -> str:
    token = path.read_text().strip()
    if not token:
        die(f"empty token file: {path}")
    return token


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def matrix_request(config: Config, method: str, path: str, params: dict[str, Any] | None = None, payload: Any | None = None) -> Any:
    token = load_access_token(config.access_token_file)
    url = f"{config.homeserver}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def matrix_whoami(config: Config) -> dict[str, Any]:
    return matrix_request(config, "GET", "/_matrix/client/v3/account/whoami")


def matrix_sync(config: Config, since: str | None) -> dict[str, Any]:
    params = {"timeout": str(config.sync_timeout_ms)}
    if since:
        params["since"] = since
    return matrix_request(config, "GET", "/_matrix/client/v3/sync", params=params)


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def send_magic_packet(mac: str) -> None:
    clean = mac.replace(":", "").replace("-", "")
    if len(clean) != 12:
        die(f"invalid MAC address: {mac}")
    payload = bytes.fromhex("ff" * 6 + clean * 16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in ("255.255.255.255", "192.168.1.255"):
            sock.sendto(payload, (target, 9))


def alpha_ssh_base(config: Config) -> list[str]:
    return [
        "ssh",
        "-i",
        str(config.alpha_ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=5",
        f"{config.alpha_ssh_user}@{config.alpha_host}",
    ]


def alpha_ssh_ready(config: Config) -> bool:
    try:
        subprocess.run(alpha_ssh_base(config) + ["/usr/bin/true"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False


def alpha_run(config: Config, command: str) -> bool:
    try:
        subprocess.run(alpha_ssh_base(config) + [command], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False


def ensure_alpha_awake(config: Config) -> bool:
    if not alpha_ssh_ready(config):
        log("sending Wake-on-LAN packet")
        send_magic_packet(config.alpha_mac)
    deadline = time.time() + config.wake_timeout_seconds
    while time.time() < deadline:
        if alpha_ssh_ready(config):
            alpha_run(config, f"/home/jbfly/.local/bin/keep-awake {shlex.quote(config.alpha_awake_lease)}")
            alpha_run(config, "systemctl --user start beeper-bot.service >/dev/null 2>&1 || true")
            return True
        time.sleep(2)
    return False


def event_body(event: dict[str, Any]) -> str:
    content = event.get("content")
    if not isinstance(content, dict):
        return ""
    body = content.get("body")
    return body if isinstance(body, str) else ""


def should_trigger(config: Config, event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type == "m.room.encrypted":
        return True
    if event_type != "m.room.message":
        return False
    body = event_body(event).strip()
    if not body:
        return False
    if body.startswith(config.reply_prefix):
        return False
    if body.startswith("[ALPHA] "):
        return False
    return True


def event_summary(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    sender = str(event.get("sender") or "")
    if event_type == "m.room.encrypted":
        return f"type={event_type} sender={sender}"
    body = event_body(event).strip().replace("\n", " ")
    if len(body) > 120:
        body = body[:117] + "..."
    return f"type={event_type} sender={sender} body={body}"


def room_events(sync_data: dict[str, Any], room_id: str) -> list[dict[str, Any]]:
    rooms = sync_data.get("rooms")
    if not isinstance(rooms, dict):
        return []
    join = rooms.get("join")
    if not isinstance(join, dict):
        return []
    room = join.get(room_id)
    if not isinstance(room, dict):
        return []
    timeline = room.get("timeline")
    if not isinstance(timeline, dict):
        return []
    events = timeline.get("events")
    if not isinstance(events, list):
        return []
    return [item for item in events if isinstance(item, dict)]


def handle_sync(config: Config, state: dict[str, Any], sync_data: dict[str, Any]) -> None:
    now = time.time()
    last_trigger = float(state.get("last_trigger_time", 0.0))
    triggered = False
    cooldown_hit = False
    for event in room_events(sync_data, config.room_id):
        state["last_event_type"] = str(event.get("type") or "")
        state["last_event_sender"] = str(event.get("sender") or "")
        state["last_event_id"] = str(event.get("event_id") or "")
        if not should_trigger(config, event):
            continue
        if now - last_trigger < config.trigger_cooldown_seconds:
            log("trigger seen inside cooldown window; skipping")
            cooldown_hit = True
            continue
        log(f"control-room activity detected: {event_summary(event)}")
        ok = ensure_alpha_awake(config)
        log("alpha ready" if ok else "alpha did not come up before timeout")
        state["last_trigger_time"] = now
        state["last_result"] = "triggered" if ok else "wake-timeout"
        triggered = True
        break
    state["last_sync"] = int(now)
    if not triggered:
        state["last_result"] = "cooldown" if cooldown_hit else "idle"


def cmd_status(config: Config) -> int:
    who = matrix_whoami(config)
    state = load_state(config.state_path)
    print(f"matrix_user={who.get('user_id','')}")
    print(f"room_id={config.room_id}")
    print(f"alpha_host={config.alpha_host}")
    print(f"alpha_ssh_ready={int(alpha_ssh_ready(config))}")
    for key in ("last_result", "last_sync", "last_event_type", "last_event_sender", "last_event_id", "last_trigger_time"):
        if key in state:
            print(f"{key}={state[key]}")
    return 0


def cmd_once(config: Config) -> int:
    state = load_state(config.state_path)
    since = state.get("next_batch")
    if since:
        sync_data = matrix_sync(config, since)
        handle_sync(config, state, sync_data)
    else:
        sync_data = matrix_sync(config, None)
        state["last_result"] = "primed"
        state["last_sync"] = int(time.time())
    next_batch = sync_data.get("next_batch")
    if isinstance(next_batch, str) and next_batch:
        state["next_batch"] = next_batch
    save_state(config.state_path, state)
    print(state.get("last_result", "unknown"))
    return 0


def cmd_serve(config: Config) -> int:
    state = load_state(config.state_path)
    if "next_batch" not in state:
        log("priming initial sync token")
        sync_data = matrix_sync(config, None)
        next_batch = sync_data.get("next_batch")
        if not isinstance(next_batch, str) or not next_batch:
            die("sync did not return next_batch")
        state["next_batch"] = next_batch
        state["last_result"] = "primed"
        state["last_sync"] = int(time.time())
        save_state(config.state_path, state)

    while True:
        try:
            sync_data = matrix_sync(config, str(state["next_batch"]))
            handle_sync(config, state, sync_data)
            next_batch = sync_data.get("next_batch")
            if isinstance(next_batch, str) and next_batch:
                state["next_batch"] = next_batch
            save_state(config.state_path, state)
        except urllib.error.URLError as exc:
            log(f"sync failed: {exc}; retrying")
            time.sleep(5)
        except Exception as exc:
            log(f"unexpected error: {exc}; retrying")
            time.sleep(5)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("command", choices=["status", "once", "serve"])
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(Path(args.config))
    if args.command == "status":
        return cmd_status(config)
    if args.command == "once":
        return cmd_once(config)
    return cmd_serve(config)


if __name__ == "__main__":
    raise SystemExit(main())
