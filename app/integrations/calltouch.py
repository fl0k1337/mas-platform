"""
Адаптер Calltouch — выгрузка журнала звонков.

Где клиент берёт доступ: Calltouch → Интеграции → API → скопировать
45-значный токен (clientApiId) и ID сайта (siteId).

Метод: GET https://api.calltouch.ru/calls-service/RestAPI/{siteId}/calls-diary/calls
Ответ бывает двух видов: за один день — просто список [...], за период —
объект с постраничным полем "records". Обрабатываем оба.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.integrations.base import CTInteraction, normalize_phone


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _parse_call(raw: dict) -> CTInteraction:
    return CTInteraction(
        external_id=str(raw.get("callId", "")),
        phone_e164=normalize_phone(raw.get("callerNumber")),
        is_target=bool(raw.get("targetCall", False)),
        is_unique=bool(raw.get("uniqueCall", False)),
        source=raw.get("utmSource") or raw.get("source"),
        duration_sec=int(raw["duration"]) if raw.get("duration") else None,
        occurred_at=_parse_dt(raw.get("date")) or datetime.now(),
    )


class CalltouchAdapter:
    kind = "calltouch"

    def __init__(self, token: str, site_id: str):
        self.token = token.strip()
        self.site_id = str(site_id).strip()
        self.url = (f"https://api.calltouch.ru/calls-service/RestAPI/"
                    f"{self.site_id}/calls-diary/calls")

    def _fetch(self, since: datetime, until: datetime) -> list[dict]:
        params = {
            "clientApiId": self.token,
            "dateFrom": since.strftime("%d/%m/%Y"),
            "dateTo": until.strftime("%d/%m/%Y"),
            "page": 1, "pageSize": 1000,
        }
        records: list[dict] = []
        while True:
            resp = httpx.get(self.url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):        # ответ за один день
                records.extend(data)
                break
            records.extend(data.get("records", []))
            if params["page"] >= data.get("pageTotal", 1) or len(records) >= 5000:
                break
            params["page"] += 1
        return records

    def test_connection(self) -> tuple[bool, str]:
        try:
            now = datetime.now()
            self._fetch(now, now)             # пустой день — норм, главное 200
            return True, "подключение к Calltouch успешно"
        except Exception as e:
            return False, f"ошибка Calltouch: {e}"

    def get_calls(self, since: datetime) -> list[CTInteraction]:
        raw = self._fetch(since, datetime.now())
        return [_parse_call(r) for r in raw]
