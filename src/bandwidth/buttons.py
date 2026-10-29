"""Telegram inline buttons: namespaced button data, so one bot can carry the buttons of several tools (SPEC BOT-2)."""

from __future__ import annotations

PREFIX_ROOM = 3      # every prefix is two letters and a colon
MAX_DATA = 64 - PREFIX_ROOM  # Telegram's limit is 64 bytes, including the prefix


def prefix_markup(markup: dict | None, prefix: str) -> dict | None:
    """A copy of an inline keyboard with `prefix` before every callback_data (added once)."""
    if not markup or "inline_keyboard" not in markup or not prefix:
        return markup
    rows = []
    for row in markup["inline_keyboard"]:
        out = []
        for button in row:
            data = button.get("callback_data")
            if data is not None and not data.startswith(prefix):
                button = {**button, "callback_data": prefix + data}
            out.append(button)
        rows.append(out)
    return {**markup, "inline_keyboard": rows}


def keyboard(*rows: list[tuple[str, str]]) -> dict:
    """Inline keyboard from rows of (label, callback_data); data must leave room for the prefix."""
    for row in rows:
        for _, data in row:
            if len(data.encode()) > MAX_DATA:
                raise ValueError(f"callback data too long: {data!r}")
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}
