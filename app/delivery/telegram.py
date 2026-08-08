"""Канал Telegram — РАБОЧИЙ. Публикация в канал клиента ботом платформы."""

from __future__ import annotations

from app import settings
from app.delivery.base import READY, Channel, SendResult


class TelegramChannel(Channel):
    key = "telegram"
    title = "Telegram"
    maturity = READY
    hint = ("токен бота и канал задаются на странице «Настройки»; "
            "у клиента можно указать свой канал — он важнее общего. "
            "Бот должен быть админом канала")
    fields = [("chat_id", "@канал клиента (если не общий)")]

    def _chat_id(self) -> str | None:
        return self.creds.get("chat_id") or settings.get("TELEGRAM_CHANNEL_ID")

    def configured(self) -> bool:
        return bool(settings.get("TELEGRAM_BOT_TOKEN") and self._chat_id())

    def _send(self, text: str, **kw) -> SendResult:
        import httpx
        token = settings.get("TELEGRAM_BOT_TOKEN")
        resp = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": self._chat_id(), "text": text}, timeout=20)
        data = resp.json()
        if not data.get("ok"):
            return SendResult(False, f"Telegram отказал: {data.get('description')}")
        return SendResult(True, f"опубликовано в {self._chat_id()}",
                          str(data["result"]["message_id"]))
