"""Sending. One POST, no client class, no new dependency — ``requests`` is already
declared in ``pyproject.toml``.

Failures raise. An alert that silently did not arrive is the exact failure the
heartbeat one file over exists to make impossible, so swallowing a send error
here would defeat that safeguard.
"""

import requests

from ..config import (
    ALERT_DRY_RUN,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TIMEOUT_SECONDS,
)

API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram rejects messages over 4096 characters outright. An LLM-written risk
# list has no length bound, so split rather than truncate: the footer carrying
# the dossier command sits at the end, and that is the part a truncation eats.
MAX_CHARS = 4000


class TelegramError(RuntimeError):
    """The send did not happen. Never raised for a message nobody tried to send."""


def _chunks(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        piece = line[:MAX_CHARS]
        if current and len(current) + len(piece) + 1 > MAX_CHARS:
            parts.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts


def send(
    text: str,
    *,
    token: str = TELEGRAM_BOT_TOKEN,
    chat_id: str = TELEGRAM_CHAT_ID,
    dry_run: bool = ALERT_DRY_RUN,
    session=None,
) -> bool:
    """Send one message. Returns True if it actually went out over the network.

    No token configured prints to stdout and returns False — the development
    path, loud by design. A configured token that fails raises: the caller must
    not record as sent something that was not.
    """
    if dry_run or not token or not chat_id:
        reason = "dry run" if dry_run else "no VALUE_TELEGRAM_BOT_TOKEN/CHAT_ID"
        print(f"--- telegram ({reason}) ---\n{text}\n---")
        return False

    poster = session.post if session is not None else requests.post
    for chunk in _chunks(text):
        try:
            response = poster(
                API.format(token=token),
                json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise TelegramError(f"telegram send failed: {exc}") from exc
        if response.status_code != 200:
            raise TelegramError(
                f"telegram returned {response.status_code}: {response.text[:200]}"
            )
    return True
