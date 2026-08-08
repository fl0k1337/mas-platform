"""
Слой отправки (Delivery Layer) — единый способ доставить готовый контент
в любой канал: Telegram, SMS, WhatsApp, MAX, Instagram.

Устроен так же, как слой интеграций: один интерфейс, много реализаций.
Панель и агенты зовут `deliver(tenant_id, channel, text)` и НЕ знают,
как устроен конкретный канал.

Три состояния канала:
  • READY   — настроен, отправляет по-настоящему;
  • DRAFT   — заготовка: код запроса написан, но канал ещё не подключён
              (нет доступов/договора). Возвращает понятное «не настроено»;
  • BLOCKED — сознательно отключён (юридический риск), включается вручную.

Принцип платформы: канал НИКОГДА не делает вид, что отправил. Если отправки
не было — это видно и в панели, и в уведомлении.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

READY, DRAFT, BLOCKED = "ready", "draft", "blocked"


@dataclass
class SendResult:
    ok: bool
    message: str                 # человекочитаемо: что произошло
    external_id: str | None = None   # id сообщения во внешней системе


class Channel(ABC):
    """Контракт канала отправки."""

    key: str = "base"
    title: str = "Канал"
    maturity: str = DRAFT        # READY | DRAFT | BLOCKED
    hint: str = ""               # что нужно, чтобы канал заработал

    def __init__(self, creds: dict | None = None):
        self.creds = creds or {}

    @abstractmethod
    def configured(self) -> bool:
        """Есть ли всё необходимое для реальной отправки."""

    @abstractmethod
    def _send(self, text: str, **kw) -> SendResult:
        """Собственно отправка. Вызывается только если configured() истинно."""

    def send(self, text: str, dry_run: bool = False, **kw) -> SendResult:
        """Единая точка: проверяет готовность, уважает режим репетиции."""
        if self.maturity == BLOCKED:
            return SendResult(False, f"{self.title}: канал отключён — {self.hint}")
        if not self.configured():
            return SendResult(False, f"{self.title}: не настроен — {self.hint}")
        if dry_run or os.getenv("DELIVERY_DRY_RUN", "").strip() in ("1", "true", "yes"):
            preview = text[:60].replace("\n", " ")
            return SendResult(True, f"{self.title}: РЕПЕТИЦИЯ, отправки не было "
                                    f"(«{preview}…», {len(text)} симв.)", None)
        try:
            return self._send(text, **kw)
        except Exception as e:
            return SendResult(False, f"{self.title}: ошибка отправки — {e}")


# --------------------------------------------------------------- реестр ---

def registry() -> dict[str, type[Channel]]:
    """Все известные каналы. Импорт внутри — чтобы не было циклов."""
    from app.delivery.instagram import InstagramChannel
    from app.delivery.max_bot import MaxChannel
    from app.delivery.sms import SmsChannel
    from app.delivery.telegram import TelegramChannel
    from app.delivery.whatsapp import WhatsAppChannel
    return {c.key: c for c in (TelegramChannel, SmsChannel, WhatsAppChannel,
                               MaxChannel, InstagramChannel)}


def build_channel(tenant_id: int, channel_key: str) -> Channel | None:
    """Собрать канал клиента: доступы берутся из таблицы integrations
    (kind = 'channel_<ключ>'), а для Telegram — ещё и из общего .env."""
    from app import db
    cls = registry().get(channel_key)
    if cls is None:
        return None
    integ = db.get_integration(tenant_id, f"channel_{channel_key}")
    return cls((integ or {}).get("credentials", {}))


def deliver(tenant_id: int, channel_key: str, text: str, **kw) -> SendResult:
    """Отправить текст в канал клиента. Главная точка входа слоя."""
    ch = build_channel(tenant_id, channel_key)
    if ch is None:
        return SendResult(False, f"канал «{channel_key}» не поддерживается")
    return ch.send(text, **kw)


def channels_status(tenant_id: int) -> list[dict]:
    """Состояние всех каналов клиента — для панели."""
    out = []
    for key, cls in registry().items():
        ch = build_channel(tenant_id, key)
        out.append({
            "key": key, "title": cls.title, "maturity": cls.maturity,
            "hint": cls.hint, "configured": bool(ch and ch.configured()),
            "fields": getattr(cls, "fields", []),
        })
    return out
