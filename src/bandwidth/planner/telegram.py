"""Telegram Bot API client: messages, photos, edits, files, updates (SPEC MSG-1, IN-4, SAF-1)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from bandwidth.buttons import keyboard, prefix_markup  # noqa: F401 - keyboard is used via this module

from .config import redact

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


class Telegram:
    """Thin Bot API client. Errors never contain the token."""

    callback_prefix = "pl:"  # this tool's buttons in the shared bot (SPEC BOT-2)

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        if not token or not chat_id:
            raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
        self.token, self.chat_id = token, str(chat_id)
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, read=70))

    def call(self, method: str, data: dict | None = None, files: dict | None = None, timeout: float | None = None) -> Any:
        url = API.format(token=self.token, method=method)
        if data and data.get("reply_markup"):
            data = {**data, "reply_markup": prefix_markup(data["reply_markup"], self.callback_prefix)}
        try:
            if files:
                form = {k: json.dumps(v) if isinstance(v, dict | list) else str(v) for k, v in (data or {}).items()}
                resp = self.client.post(url, data=form, files=files, timeout=timeout or 60)
            else:
                resp = self.client.post(url, json=data or {}, timeout=timeout or 30)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(redact(f"telegram {method} failed: {e}").replace(self.token, "***")) from None
        if not isinstance(body, dict) or not body.get("ok"):
            desc = body.get("description") if isinstance(body, dict) else body
            raise TelegramError(f"telegram {method} rejected: {desc}")
        return body.get("result")

    def send(self, text: str, markup: dict | None = None, silent: bool = False) -> int:
        data: dict[str, Any] = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}
        if markup:
            data["reply_markup"] = markup
        if silent:
            data["disable_notification"] = True
        return int(self.call("sendMessage", data)["message_id"])

    def send_photo(self, png: bytes, caption: str, markup: dict | None = None) -> int:
        data: dict[str, Any] = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
        if markup:
            data["reply_markup"] = markup
        return int(self.call("sendPhoto", data, files={"photo": ("plan.png", png, "image/png")})["message_id"])

    def _ignore_unchanged(self, method: str, data: dict) -> None:
        try:
            self.call(method, data)
        except TelegramError as e:
            if "message is not modified" not in str(e):
                raise

    def edit_text(self, message_id: int, text: str, markup: dict | None = None) -> None:
        self._ignore_unchanged("editMessageText", {
            "chat_id": self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": markup or {"inline_keyboard": []}})

    def edit_caption(self, message_id: int, caption: str, markup: dict | None = None) -> None:
        self._ignore_unchanged("editMessageCaption", {
            "chat_id": self.chat_id, "message_id": message_id, "caption": caption, "parse_mode": "HTML",
            "reply_markup": markup or {"inline_keyboard": []}})

    def answer(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def send_file(self, filename: str, content: bytes, caption: str = "", mime: str = "text/calendar") -> None:
        self.call("sendDocument", {"chat_id": self.chat_id, "caption": caption},
                  files={"document": (filename, content, mime)})

    def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        data: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            data["offset"] = offset
        result = self.call("getUpdates", data, timeout=timeout + 15)
        return result if isinstance(result, list) else []
