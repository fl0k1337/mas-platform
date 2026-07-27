"""
Слой интеграций (Integration Layer) — сердце универсальности продукта.

Любая CRM подключается через ОДИН интерфейс CRMAdapter и наружу отдаёт
УНИФИЦИРОВАННЫЕ модели (UnifiedLead, UnifiedDeal, UnifiedContact). Агенты
(leads.py, finance.py) работают только с этими моделями и НЕ знают, какая CRM
подключена у клиента. Добавить amoCRM/RetailCRM/HubSpot = написать ещё один
класс-адаптер, не трогая агентов. Это паттерн Adapter из спецификации.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel

try:
    import phonenumbers
    _HAS_PN = True
except ImportError:
    _HAS_PN = False


def normalize_phone(raw: str | None) -> str | None:
    """Любой формат телефона -> +79251112233. None, если не распознать."""
    if not raw:
        return None
    raw = str(raw)
    if "скрыт" in raw.lower():
        return None
    if _HAS_PN:
        try:
            p = phonenumbers.parse(raw, "RU")
            if phonenumbers.is_valid_number(p):
                return phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


# ----------------------------- унифицированные модели ------------------------

class UnifiedContact(BaseModel):
    external_id: str
    name: str | None = None
    phones: list[str] = []        # нормализованные E.164


class UnifiedLead(BaseModel):
    external_id: str
    phone_e164: str | None = None
    source: str | None = None
    unified_status: str           # NEW | IN_PROGRESS | QUALIFIED | REJECTED
    raw_status: str = ""
    responsible: str | None = None
    amount: float | None = None
    created_at: datetime
    updated_at: datetime | None = None


class CTInteraction(BaseModel):
    """Обращение из Calltouch (звонок). Для сверки с лидами CRM."""
    external_id: str
    phone_e164: str | None = None
    is_target: bool = False       # целевой ли звонок
    is_unique: bool = False
    source: str | None = None
    duration_sec: int | None = None
    occurred_at: datetime


class UnifiedDeal(BaseModel):
    external_id: str
    unified_stage: str            # NEW | QUALIFIED | PROPOSAL | WON | LOST
    raw_stage: str = ""
    amount: float | None = None
    currency: str = "RUB"
    responsible: str | None = None
    created_at: datetime
    closed_at: datetime | None = None


# ----------------------------- контракт адаптера -----------------------------

class CRMAdapter(ABC):
    """Единый интерфейс для любой CRM. Каждая CRM — свой класс-наследник."""

    kind: str = "crm_base"

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Проверка доступа. Возвращает (успех, человекочитаемое сообщение)."""

    @abstractmethod
    def get_leads(self, since: datetime) -> list[UnifiedLead]: ...

    @abstractmethod
    def get_deals(self, since: datetime) -> list[UnifiedDeal]: ...
