"""
Адаптер Битрикс24 через «входящий вебхук» (самый простой доступ, без OAuth).

Как клиент получает данные для подключения:
  Битрикс24 → Разработчикам → Другое → Входящий вебхук → выдать права на CRM →
  скопировать URL вида: https://ПОРТАЛ.bitrix24.ru/rest/1/ТОКЕН/

Мы дергаем методы crm.lead.list и crm.deal.list, разбираем ответ и приводим
к унифицированным моделям. Разбор ответа (_parse_*) отделён от сети — его
легко тестировать на фейковых данных.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.integrations.base import (CRMAdapter, UnifiedDeal, UnifiedLead,
                                    normalize_phone)

# Маппинг стадий Битрикса в наши унифицированные статусы.
# У каждого клиента могут быть свои воронки — это дефолт, потом настраивается.
LEAD_STATUS_MAP = {
    "NEW": "NEW", "IN_PROCESS": "IN_PROGRESS", "PROCESSED": "IN_PROGRESS",
    "CONVERTED": "QUALIFIED", "JUNK": "REJECTED",
}
DEAL_STAGE_MAP = {
    "NEW": "NEW", "PREPARATION": "QUALIFIED", "PREPAYMENT_INVOICE": "PROPOSAL",
    "EXECUTING": "PROPOSAL", "WON": "WON", "LOSE": "LOST",
}


def _parse_dt(raw: str | None) -> datetime | None:
    """Битрикс отдаёт даты в ISO-8601 с зоной, напр. 2026-07-01T10:00:00+03:00."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _first_phone(raw_lead: dict) -> str | None:
    """Телефон в Битриксе — мультиполе PHONE: [{'VALUE': '...', ...}, ...]."""
    phones = raw_lead.get("PHONE") or []
    for ph in phones:
        e164 = normalize_phone(ph.get("VALUE"))
        if e164:
            return e164
    return None


def _map_status(raw_code: str, custom: dict, default: dict, fallback: str) -> str:
    """Сначала смотрим настроенный клиентом маппинг, потом дефолт, потом fallback."""
    return custom.get(raw_code) or default.get(raw_code) or fallback


def _parse_lead(raw: dict, lead_map: dict | None = None) -> UnifiedLead:
    raw_status = raw.get("STATUS_ID", "")
    return UnifiedLead(
        external_id=str(raw.get("ID", "")),
        phone_e164=_first_phone(raw),
        source=raw.get("SOURCE_ID"),
        unified_status=_map_status(raw_status, lead_map or {}, LEAD_STATUS_MAP, "IN_PROGRESS"),
        raw_status=raw_status,
        responsible=str(raw.get("ASSIGNED_BY_ID")) if raw.get("ASSIGNED_BY_ID") else None,
        amount=float(raw["OPPORTUNITY"]) if raw.get("OPPORTUNITY") else None,
        created_at=_parse_dt(raw.get("DATE_CREATE")) or datetime.now(),
        updated_at=_parse_dt(raw.get("DATE_MODIFY")),
    )


def _parse_deal(raw: dict, deal_map: dict | None = None) -> UnifiedDeal:
    raw_stage = raw.get("STAGE_ID", "")
    return UnifiedDeal(
        external_id=str(raw.get("ID", "")),
        unified_stage=_map_status(raw_stage, deal_map or {}, DEAL_STAGE_MAP, "QUALIFIED"),
        raw_stage=raw_stage,
        amount=float(raw["OPPORTUNITY"]) if raw.get("OPPORTUNITY") else None,
        currency=raw.get("CURRENCY_ID", "RUB"),
        responsible=str(raw.get("ASSIGNED_BY_ID")) if raw.get("ASSIGNED_BY_ID") else None,
        created_at=_parse_dt(raw.get("DATE_CREATE")) or datetime.now(),
        closed_at=_parse_dt(raw.get("CLOSEDATE")),
    )


class Bitrix24Adapter(CRMAdapter):
    kind = "crm_bitrix24"

    def __init__(self, webhook_url: str, lead_map: dict | None = None,
                 deal_map: dict | None = None):
        # гарантируем один слэш на конце
        self.base = webhook_url.strip().rstrip("/") + "/"
        self.lead_map = lead_map or {}     # маппинг стадий клиента (из панели)
        self.deal_map = deal_map or {}

    def _call(self, method: str, params: dict) -> list[dict]:
        """Вызов метода Битрикса с пагинацией (поле 'next' в ответе)."""
        results: list[dict] = []
        start = 0
        while True:
            resp = httpx.post(f"{self.base}{method}.json",
                              json={**params, "start": start}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(data.get("error_description") or data["error"])
            results.extend(data.get("result", []))
            nxt = data.get("next")
            if nxt is None or len(results) >= 1000:   # предохранитель
                break
            start = nxt
        return results

    def test_connection(self) -> tuple[bool, str]:
        try:
            resp = httpx.post(f"{self.base}profile.json", timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                return False, data.get("error_description", data["error"])
            name = data.get("result", {}).get("NAME", "пользователь")
            return True, f"подключение успешно (портал: {name})"
        except Exception as e:
            return False, f"ошибка подключения: {e}"

    def get_leads(self, since: datetime) -> list[UnifiedLead]:
        raw = self._call("crm.lead.list", {
            "filter": {">=DATE_CREATE": since.strftime("%Y-%m-%dT%H:%M:%S")},
            "select": ["ID", "STATUS_ID", "SOURCE_ID", "ASSIGNED_BY_ID",
                       "OPPORTUNITY", "DATE_CREATE", "DATE_MODIFY", "PHONE"],
            "order": {"DATE_CREATE": "DESC"},
        })
        return [_parse_lead(r, self.lead_map) for r in raw]

    def get_deals(self, since: datetime) -> list[UnifiedDeal]:
        raw = self._call("crm.deal.list", {
            "filter": {">=DATE_CREATE": since.strftime("%Y-%m-%dT%H:%M:%S")},
            "select": ["ID", "STAGE_ID", "OPPORTUNITY", "CURRENCY_ID",
                       "ASSIGNED_BY_ID", "DATE_CREATE", "CLOSEDATE"],
            "order": {"DATE_CREATE": "DESC"},
        })
        return [_parse_deal(r, self.deal_map) for r in raw]

    def get_stages(self) -> list[dict]:
        """Реальные стадии портала клиента (для настройки маппинга в панели).
        crm.status.list отдаёт справочник: лид-статусы (ENTITY_ID='STATUS')
        и стадии сделок (ENTITY_ID начинается с 'DEAL_STAGE')."""
        raw = self._call("crm.status.list", {"order": {"SORT": "ASC"}})
        stages = []
        for s in raw:
            entity_id = s.get("ENTITY_ID", "")
            if entity_id == "STATUS":
                entity = "lead"
            elif entity_id.startswith("DEAL_STAGE"):
                entity = "deal"
            else:
                continue
            stages.append({"entity": entity, "raw_code": s.get("STATUS_ID", ""),
                           "name": s.get("NAME", "")})
        return stages
