"""
Канал WhatsApp — ЗАГОТОВКА (через провайдера-агрегатора, напр. Wazzup / edna).

Почему через агрегатора, а не напрямую: официальный WhatsApp Business API
требует верификации бизнеса в Meta, а рассылочные сообщения идут только
по ШАБЛОНАМ, заранее прошедшим модерацию, — свободный текст вне 24-часового
окна диалога отправить нельзя. Агрегаторы берут это на себя.

Поэтому в коде есть поле «шаблон»: если он указан, отправляем как шаблонное
сообщение, иначе — как обычное (сработает только в открытом диалоге).
"""

from __future__ import annotations

from app.delivery.base import DRAFT, Channel, SendResult


class WhatsAppChannel(Channel):
    key = "whatsapp"
    title = "WhatsApp"
    maturity = DRAFT
    hint = ("нужен аккаунт у провайдера (Wazzup / edna / 360dialog), "
            "подключённый номер и утверждённый шаблон рассылки")
    fields = [("api_url", "URL API провайдера"),
              ("api_key", "ключ доступа"),
              ("channel_id", "id подключённого номера"),
              ("template", "имя утверждённого шаблона (необязательно)")]

    def configured(self) -> bool:
        return all(self.creds.get(k) for k in ("api_url", "api_key", "channel_id"))

    def _send(self, text: str, phone: str | None = None, **kw) -> SendResult:
        if not phone:
            return SendResult(False, "WhatsApp: не указан номер получателя")
        import httpx
        payload = {"channelId": self.creds["channel_id"],
                   "chatId": phone.lstrip("+"), "chatType": "whatsapp", "text": text}
        if self.creds.get("template"):
            payload["templateName"] = self.creds["template"]
        resp = httpx.post(self.creds["api_url"].rstrip("/") + "/message",
                          json=payload, timeout=25,
                          headers={"Authorization": f"Bearer {self.creds['api_key']}"})
        if resp.status_code >= 300:
            return SendResult(False, f"WhatsApp: провайдер вернул {resp.status_code} — "
                                     f"{resp.text[:200]}")
        data = resp.json() if resp.content else {}
        return SendResult(True, f"WhatsApp: отправлено на {phone}",
                          str(data.get("messageId") or "") or None)
