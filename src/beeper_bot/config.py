from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "beeper-bot" / "config.toml"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "beeper-bot"
DEFAULT_DB_PATH = DEFAULT_STATE_DIR / "archive.sqlite3"
DEFAULT_LOCK_PATH = DEFAULT_STATE_DIR / "serve.lock"
DEFAULT_BEEPER_API_BASE = "http://127.0.0.1:23373/v1"
DEFAULT_LLM_BASE_URL = "http://127.0.0.1:8090/v1"
DEFAULT_LLM_MODEL = "gemma"
DEFAULT_REPLY_PREFIX = "[BEEPER-BOT] "


@dataclass(slots=True)
class BeeperConfig:
    api_base: str = DEFAULT_BEEPER_API_BASE
    token_file: Path = Path.home() / ".config" / "beeper-bot" / "token"
    credentials_file: Path | None = None
    control_chat_id: str = ""
    indexed_chat_ids: list[str] = field(default_factory=list)
    poll_seconds: int = 5
    sync_interval_seconds: int = 300
    history_fetch_limit: int = 500
    history_backfill_pages: int = 20
    http_timeout_seconds: int = 30
    auto_index_recent_days: int = 0
    auto_index_max_chats: int = 100
    # transport backend: "desktop-api" (Beeper Desktop HTTP API, default) or
    # "matrix" (direct matrix-nio client on venus). See matrix_transport.py.
    transport: str = "desktop-api"
    matrix_credentials_file: Path | None = None
    matrix_store_path: Path | None = None


@dataclass(slots=True)
class ArchiveConfig:
    path: Path = DEFAULT_DB_PATH


@dataclass(slots=True)
class LlmConfig:
    base_url: str = DEFAULT_LLM_BASE_URL
    model: str = DEFAULT_LLM_MODEL
    planner_base_url: str = ""
    planner_model: str = ""
    planner_temperature: float | None = None
    planner_max_output_tokens: int = 0
    timeout_seconds: int = 120
    max_input_snippets: int = 10
    max_output_tokens: int = 300
    temperature: float = 0.1


@dataclass(slots=True)
class BridgeConfig:
    reply_prefix: str = DEFAULT_REPLY_PREFIX
    max_reply_chars: int = 3500
    max_reply_parts: int = 6
    send_ack: bool = True


@dataclass(slots=True)
class MediaConfig:
    exclude_chat_ids: list[str] = field(default_factory=list)
    auto_derive: bool = True
    auto_derive_per_cycle: int = 3


@dataclass(slots=True)
class ChatSetConfig:
    name: str
    display_name: str
    aliases: list[str] = field(default_factory=list)
    chats: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SecurityConfig:
    allow_web_search: bool = False
    log_raw_messages: bool = False


@dataclass(slots=True)
class AppConfig:
    config_path: Path
    beeper: BeeperConfig = field(default_factory=BeeperConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    chat_sets: dict[str, ChatSetConfig] = field(default_factory=dict)
    security: SecurityConfig = field(default_factory=SecurityConfig)

    @property
    def state_dir(self) -> Path:
        return self.archive.path.parent

    @property
    def lock_path(self) -> Path:
        return DEFAULT_LOCK_PATH if self.state_dir == DEFAULT_STATE_DIR else self.state_dir / "serve.lock"


class ConfigError(RuntimeError):
    pass


def _get_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"Config section [{key}] must be a table")
    return value


def _path_value(section: dict[str, Any], key: str, default: Path) -> Path:
    value = section.get(key)
    if value in (None, ""):
        return default
    return Path(os.path.expanduser(str(value)))


def _int_value(section: dict[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    return int(value)


def _float_value(section: dict[str, Any], key: str, default: float) -> float:
    value = section.get(key, default)
    return float(value)


def _optional_float_value(section: dict[str, Any], key: str) -> float | None:
    value = section.get(key)
    if value in (None, ""):
        return None
    return float(value)


def _bool_value(section: dict[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"Config key {key!r} must be a boolean")


def _str_list_value(section: dict[str, Any], key: str, default: list[str] | None = None) -> list[str]:
    value = section.get(key, default or [])
    if not isinstance(value, list):
        raise ConfigError(f"Config key {key!r} must be a list")
    return [str(item) for item in value]


def _chat_sets_value(raw: dict[str, Any]) -> dict[str, ChatSetConfig]:
    result: dict[str, ChatSetConfig] = {}
    for key, value in raw.items():
        name = str(key)
        fallback_name = name.replace("_", " ")
        if isinstance(value, list):
            result[name] = ChatSetConfig(
                name=name,
                display_name=fallback_name,
                aliases=[fallback_name],
                chats=[str(item) for item in value],
            )
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"Config section [chat_sets.{name}] must be a table or list")
        display_name = str(value.get("display_name", fallback_name) or fallback_name)
        result[name] = ChatSetConfig(
            name=name,
            display_name=display_name,
            aliases=_str_list_value(value, "aliases", [fallback_name, display_name]),
            chats=_str_list_value(value, "chats", []),
        )
    return result


def load_config(path: Path | str | None = None) -> AppConfig:
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)

    beeper_raw = _get_table(raw, "beeper")
    archive_raw = _get_table(raw, "archive")
    llm_raw = _get_table(raw, "llm")
    bridge_raw = _get_table(raw, "bridge")
    media_raw = _get_table(raw, "media")
    chat_sets_raw = _get_table(raw, "chat_sets")
    security_raw = _get_table(raw, "security")

    config = AppConfig(
        config_path=config_path,
        beeper=BeeperConfig(
            api_base=str(beeper_raw.get("api_base", DEFAULT_BEEPER_API_BASE)),
            token_file=_path_value(beeper_raw, "token_file", Path.home() / ".config" / "beeper-bot" / "token"),
            credentials_file=_path_value(beeper_raw, "credentials_file", Path("")) if beeper_raw.get("credentials_file") else None,
            control_chat_id=str(beeper_raw.get("control_chat_id", "")),
            indexed_chat_ids=_str_list_value(beeper_raw, "indexed_chat_ids", []),
            poll_seconds=_int_value(beeper_raw, "poll_seconds", 5),
            sync_interval_seconds=_int_value(beeper_raw, "sync_interval_seconds", 300),
            history_fetch_limit=_int_value(beeper_raw, "history_fetch_limit", 500),
            history_backfill_pages=_int_value(beeper_raw, "history_backfill_pages", 20),
            http_timeout_seconds=_int_value(beeper_raw, "http_timeout_seconds", 30),
            auto_index_recent_days=_int_value(beeper_raw, "auto_index_recent_days", 0),
            auto_index_max_chats=_int_value(beeper_raw, "auto_index_max_chats", 100),
            transport=str(beeper_raw.get("transport", "desktop-api")),
            matrix_credentials_file=_path_value(beeper_raw, "matrix_credentials_file", Path("")) if beeper_raw.get("matrix_credentials_file") else None,
            matrix_store_path=_path_value(beeper_raw, "matrix_store_path", Path("")) if beeper_raw.get("matrix_store_path") else None,
        ),
        archive=ArchiveConfig(
            path=_path_value(archive_raw, "path", DEFAULT_DB_PATH),
        ),
        llm=LlmConfig(
            base_url=str(llm_raw.get("base_url", DEFAULT_LLM_BASE_URL)),
            model=str(llm_raw.get("model", DEFAULT_LLM_MODEL)),
            planner_base_url=str(llm_raw.get("planner_base_url", "") or ""),
            planner_model=str(llm_raw.get("planner_model", "") or ""),
            planner_temperature=_optional_float_value(llm_raw, "planner_temperature"),
            planner_max_output_tokens=_int_value(llm_raw, "planner_max_output_tokens", 0),
            timeout_seconds=_int_value(llm_raw, "timeout_seconds", 120),
            max_input_snippets=_int_value(llm_raw, "max_input_snippets", 10),
            max_output_tokens=_int_value(llm_raw, "max_output_tokens", 300),
            temperature=_float_value(llm_raw, "temperature", 0.1),
        ),
        bridge=BridgeConfig(
            reply_prefix=str(bridge_raw.get("reply_prefix", DEFAULT_REPLY_PREFIX)),
            max_reply_chars=_int_value(bridge_raw, "max_reply_chars", 3500),
            max_reply_parts=_int_value(bridge_raw, "max_reply_parts", 6),
            send_ack=_bool_value(bridge_raw, "send_ack", True),
        ),
        media=MediaConfig(
            exclude_chat_ids=_str_list_value(media_raw, "exclude_chat_ids", []),
            auto_derive=_bool_value(media_raw, "auto_derive", True),
            auto_derive_per_cycle=_int_value(media_raw, "auto_derive_per_cycle", 3),
        ),
        chat_sets=_chat_sets_value(chat_sets_raw),
        security=SecurityConfig(
            allow_web_search=_bool_value(security_raw, "allow_web_search", False),
            log_raw_messages=_bool_value(security_raw, "log_raw_messages", False),
        ),
    )

    ensure_private_dir(config.archive.path.parent)
    restrict_existing_file(config.config_path)
    restrict_existing_file(config.beeper.token_file)
    return config


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass


def ensure_private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def restrict_existing_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
