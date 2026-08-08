"""
Канал MAX — ЗАГОТОВКА на официальном Bot API мессенджера MAX.

Структура запроса реальная (эндпоинт platform-api2.max.ru, токен бота,
отправка в чат/канал). Чего не хватает: сам токен — боты в MAX публикуются
через верифицированное юридическое лицо РФ, это организационный шаг.

Лимит платформы — порядка 30 запросов в секунду, для наших объёмов запас
огромный, поэтому очередей здесь не делаем.
"""

from __future__ import annotations

from app.delivery.base import DRAFT, Channel, SendResult

API = "https://platform-api2.max.ru"


class MaxChannel(Channel):
    key = "max"
    title = "MAX"
    maturity = DRAFT
    hint = ("нужен токен бота MAX (публикация бота — через верифицированное "
            "юрлицо РФ) и id чата/канала для публикации")
    fields = [("token", "токен бота MAX"),
              ("chat_id", "id чата или канала")]

    def configured(self) -> bool:
        return bool(self.creds.get("token") and self.creds.get("chat_id"))

    def _send(self, text: str, **kw) -> SendResult:
        import httpx
        resp = httpx.post(f"{API}/messages",
                          params={"access_token": self.creds["token"],
                                  "chat_id": self.creds["chat_id"]},
                          json={"text": text}, timeout=25)
        if resp.status_code >= 300:
            return SendResult(False, f"MAX вернул {resp.status_code} — {resp.text[:200]}")
        data = resp.json() if resp.content else {}
        mid = ((data.get("message") or {}).get("body") or {}).get("mid")
        return SendResult(True, f"опубликовано в MAX ({self.creds['chat_id']})",
                          str(mid) if mid else None)
