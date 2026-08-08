"""Telegram-адаптер: уведомления владельцу и публикация в канал.

Токены читаются В МОМЕНТ ОТПРАВКИ через app.settings — поэтому изменение
настроек в панели действует сразу, без перезапуска служб.
"""

from __future__ import annotations

from app import settings

API = "https://api.telegram.org"


def _send(chat_id: str, text: str) -> dict:
    import httpx
    resp = httpx.post(f"{API}/bot{settings.get('TELEGRAM_BOT_TOKEN')}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()["result"]


def notify(text: str) -> str:
    """Сообщение владельцу в личку (отчёты, алерты)."""
    token, chat_id = settings.get("TELEGRAM_BOT_TOKEN"), settings.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return "skipped: Telegram не настроен (страница «Настройки»)"
    try:
        for i in range(0, len(text), 4000):
            _send(chat_id, text[i:i + 4000])
        return "sent"
    except Exception as e:
        return f"error: {e}"


def publish_to_channel(text: str) -> tuple[str, str | None]:
    """Публикация согласованного поста в канал.
    Возвращает (статус-сообщение, id поста в канале или None)."""
    token = settings.get("TELEGRAM_BOT_TOKEN")
    channel = settings.get("TELEGRAM_CHANNEL_ID")
    if not token:
        return "НЕ ОПУБЛИКОВАНО: не задан токен бота (страница «Настройки»)", None
    if not channel:
        return "НЕ ОПУБЛИКОВАНО: не задан канал (страница «Настройки»)", None
    try:
        msg = _send(channel, text)
        return f"опубликовано в {channel}", str(msg["message_id"])
    except Exception as e:
        return f"НЕ ОПУБЛИКОВАНО: ошибка Telegram — {e}", None


def detect_chat_id() -> tuple[bool, str]:
    """Определить chat_id владельца по последнему сообщению боту.
    Используется кнопкой «Определить» на странице настроек."""
    token = settings.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "Сначала введите и сохраните токен бота"
    try:
        import httpx
        resp = httpx.get(f"{API}/bot{token}/getUpdates", timeout=20)
        data = resp.json()
        if not data.get("ok"):
            return False, f"Telegram отказал: {data.get('description')}"
        for upd in reversed(data.get("result", [])):
            chat = ((upd.get("message") or upd.get("channel_post") or {})
                    .get("chat") or {})
            if chat.get("id"):
                return True, str(chat["id"])
        return False, ("Не нашёл сообщений. Напишите своему боту в Telegram "
                       "любое сообщение и нажмите «Определить» ещё раз")
    except Exception as e:
        return False, f"Ошибка запроса: {e}"
