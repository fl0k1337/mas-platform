"""Канал Telegram — РАБОЧИЙ. Публикация в канал клиента ботом платформы."""

from __future__ import annotations

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID
from app.delivery.base import READY, Channel, SendResult


class TelegramChannel(Channel):
    key = "telegram"
    title = "Telegram"
    maturity = READY
    hint = ("нужен токен бота в .env (TELEGRAM_BOT_TOKEN) и канал: "
            "либо TELEGRAM_CHANNEL_ID в .env, либо @канал в настройках клиента; "
            "бот должен быть админом канала")
    fields = [("chat_id", "@канал клиента (если не задан общий в .env)")]

    def _chat_id(self) -> str | None:
        return self.creds.get("chat_id") or TELEGRAM_CHANNEL_ID

    def configured(self) -> bool:
        return bool(TELEGRAM_BOT_TOKEN and self._chat_id())

    def _send(self, text: str, **kw) -> SendResult:
        import httpx
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": self._chat_id(), "text": text}, timeout=20)
        data = resp.json()
        if not data.get("ok"):
            return SendResult(False, f"Telegram отказал: {data.get('description')}")
        mid = str(data["result"]["message_id"])
        return SendResult(True, f"опубликовано в {self._chat_id()}", mid)
