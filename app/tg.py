"""Telegram-адаптер: уведомления владельцу и публикация в канал."""

from __future__ import annotations

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, TELEGRAM_CHAT_ID

API = "https://api.telegram.org"


def _send(chat_id: str, text: str) -> dict:
    import httpx
    resp = httpx.post(f"{API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()["result"]


def notify(text: str) -> str:
    """Сообщение владельцу в личку (отчёты, алерты)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return "skipped: Telegram не настроен в .env"
    try:
        for i in range(0, len(text), 4000):
            _send(TELEGRAM_CHAT_ID, text[i:i + 4000])
        return "sent"
    except Exception as e:
        return f"error: {e}"


def publish_to_channel(text: str) -> tuple[str, str | None]:
    """Публикация согласованного поста в канал.
    Возвращает (статус-сообщение, id поста в канале или None)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        return "репетиция: канал не настроен (TELEGRAM_CHANNEL_ID)", None
    try:
        msg = _send(TELEGRAM_CHANNEL_ID, text)
        return f"опубликовано в {TELEGRAM_CHANNEL_ID}", str(msg["message_id"])
    except Exception as e:
        return f"ошибка публикации: {e}", None
