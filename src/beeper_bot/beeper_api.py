from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from .config import BeeperConfig


class BeeperApiError(RuntimeError):
    pass


@dataclass(slots=True)
class MessagePage:
    items: list[dict[str, Any]]
    has_more: bool
    oldest_cursor: str | None
    newest_cursor: str | None


class BeeperApiClient:
    def __init__(self, config: BeeperConfig):
        self.config = config

    def _load_token(self) -> str:
        token_path = Path(self.config.token_file)
        if token_path.exists():
            token = token_path.read_text().strip()
            if token:
                return token
            raise BeeperApiError(f"Beeper token file is empty: {token_path}")

        credentials_path = Path(self.config.credentials_file)
        try:
            with credentials_path.open() as handle:
                credentials = json.load(handle)
        except FileNotFoundError as exc:
            raise BeeperApiError(
                f"Beeper token file not found: {token_path}; legacy credentials file also missing: {credentials_path}"
            ) from exc

        for value in credentials.values():
            if value.get("server_name") == "beeper" and value.get("access_token"):
                return str(value["access_token"])
        raise BeeperApiError(
            f"No Beeper access token found in token file {token_path} or legacy credentials file {credentials_path}"
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        token = self._load_token()
        url = f"{self.config.api_base.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.config.http_timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise BeeperApiError(f"Beeper API {method} {path} failed: HTTP {exc.code} {body}") from exc
        except error.URLError as exc:
            raise BeeperApiError(f"Beeper API {method} {path} failed: {exc}") from exc

        if not body:
            return None
        return json.loads(body)

    def fetch_chat(self, chat_id: str) -> dict[str, Any]:
        quoted = parse.quote(chat_id, safe="")
        return self._request("GET", f"/chats/{quoted}")

    def fetch_messages_page(self, chat_id: str, cursor: str | None = None, direction: str | None = None) -> MessagePage:
        quoted = parse.quote(chat_id, safe="")
        query = ""
        if cursor:
            params = {"cursor": cursor}
            if direction:
                params["direction"] = direction
            query = "?" + parse.urlencode(params)
        payload = self._request("GET", f"/chats/{quoted}/messages{query}")
        if not isinstance(payload, dict):
            raise BeeperApiError(f"Unexpected Beeper message response for chat {chat_id}")
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise BeeperApiError(f"Unexpected Beeper message items for chat {chat_id}")
        return MessagePage(
            items=[item for item in items if isinstance(item, dict)],
            has_more=bool(payload.get("hasMore", False)),
            oldest_cursor=str(payload["oldestCursor"]) if payload.get("oldestCursor") not in (None, "") else None,
            newest_cursor=str(payload["newestCursor"]) if payload.get("newestCursor") not in (None, "") else None,
        )

    def fetch_messages(self, chat_id: str) -> list[dict[str, Any]]:
        return self.fetch_messages_page(chat_id).items

    def send_message(self, chat_id: str, text: str) -> None:
        quoted = parse.quote(chat_id, safe="")
        self._request("POST", f"/chats/{quoted}/messages", {"text": text})
