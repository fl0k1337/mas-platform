"""
Канал SMS — ЗАГОТОВКА с рабочей структурой запроса (провайдер SMS Aero).

Что уже сделано: формат запроса, авторизация, разбор ответа, подсчёт сегментов.
Чего не хватает для боевой работы: договор с провайдером, зарегистрированная
подпись отправителя (её проверяет оператор) и деньги на счёте. Как появятся —
вписываются в карточке клиента, код менять не нужно.

Другого провайдера (SMSC, МТС Exolve) добавить просто: это ещё один класс
с таким же интерфейсом — меняется только метод _send.
"""

from __future__ import annotations

from app.delivery.base import DRAFT, Channel, SendResult

SEGMENT_CYRILLIC = 70          # символов в одном сегменте кириллицей


def segments(text: str) -> int:
    """Сколько SMS-сегментов займёт текст (за каждый платят отдельно)."""
    return max(1, -(-len(text) // SEGMENT_CYRILLIC))


class SmsChannel(Channel):
    key = "sms"
    title = "SMS"
    maturity = DRAFT
    hint = ("нужен договор с провайдером (SMS Aero / SMSC / МТС Exolve), "
            "зарегистрированная подпись отправителя и баланс на счёте")
    fields = [("email", "email аккаунта провайдера"),
              ("api_key", "API-ключ"),
              ("sign", "подпись отправителя (зарегистрированная)")]

    def configured(self) -> bool:
        return all(self.creds.get(k) for k in ("email", "api_key", "sign"))

    def _send(self, text: str, phone: str | None = None, **kw) -> SendResult:
        if not phone:
            return SendResult(False, "SMS: не указан номер получателя")
        import httpx
        # Реальный формат SMS Aero: basic-auth (email:api_key), номер без «+»
        resp = httpx.get(
            "https://gate.smsaero.ru/v2/sms/send",
            params={"number": phone.lstrip("+"), "text": text,
                    "sign": self.creds["sign"], "channel": "DIRECT"},
            auth=(self.creds["email"], self.creds["api_key"]), timeout=25)
        data = resp.json()
        if not data.get("success"):
            return SendResult(False, f"SMS: провайдер отказал — {data.get('message')}")
        msg_id = str((data.get("data") or {}).get("id", ""))
        return SendResult(True, f"SMS отправлена на {phone} "
                                f"({segments(text)} сегм.)", msg_id or None)
