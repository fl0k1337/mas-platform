"""
Сервис интеграций: собирает нужный адаптер по данным из БД и умеет
проверять/синхронизировать CRM. Агенты и веб-панель зовут только отсюда,
не зная деталей конкретной CRM.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import db
from app.integrations.base import CRMAdapter
from app.integrations.bitrix24 import Bitrix24Adapter
from app.integrations.calltouch import CalltouchAdapter


def build_crm_adapter(tenant_id: int) -> CRMAdapter | None:
    """Вернёт готовый CRM-адаптер клиента или None, если CRM не подключена."""
    maps = db.get_stage_mappings(tenant_id)
    for kind, builder in (
        ("crm_bitrix24", lambda c: Bitrix24Adapter(
            c["webhook_url"], lead_map=maps.get("lead"), deal_map=maps.get("deal"))),
        # ("crm_amocrm", lambda c: AmoCRMAdapter(...)),  # добавится тем же способом
    ):
        integ = db.get_integration(tenant_id, kind)
        if integ and integ["credentials"].get("webhook_url"):
            return builder(integ["credentials"])
    return None


def build_calltouch_adapter(tenant_id: int) -> CalltouchAdapter | None:
    """Вернёт адаптер Calltouch клиента или None, если не подключён."""
    integ = db.get_integration(tenant_id, "calltouch")
    if integ and integ["credentials"].get("token") and integ["credentials"].get("site_id"):
        return CalltouchAdapter(integ["credentials"]["token"],
                                integ["credentials"]["site_id"])
    return None


def get_estimates(tenant_id: int) -> list[dict] | None:
    """Сметы клиента из Google Таблицы, если подключена. Иначе None.
    Может бросить исключение при проблемах доступа — вызывающий ловит."""
    integ = db.get_integration(tenant_id, "estimates_sheet")
    if not integ or not integ["credentials"].get("sheet_id"):
        return None
    from app.integrations.sheets import read_estimates
    return read_estimates(integ["credentials"]["sheet_id"],
                          integ["credentials"].get("worksheet") or None)


def test_calltouch(tenant_id: int) -> str:
    """Проверить подключение Calltouch и записать статус."""
    integ = db.get_integration(tenant_id, "calltouch")
    if not integ:
        return "интеграция не найдена"
    adapter = build_calltouch_adapter(tenant_id)
    if adapter is None:
        return "не заполнены токен и ID сайта"
    ok, msg = adapter.test_connection()
    db.set_integration_status(integ["id"], "active" if ok else "error", msg)
    return msg


def test_crm(tenant_id: int, kind: str) -> str:
    """Проверить подключение CRM и записать статус. Для кнопки в панели."""
    integ = db.get_integration(tenant_id, kind)
    if not integ:
        return "интеграция не найдена"
    adapter = build_crm_adapter(tenant_id)
    if adapter is None:
        return "не заполнены данные подключения"
    ok, msg = adapter.test_connection()
    db.set_integration_status(integ["id"], "active" if ok else "error", msg)
    return msg


def sync_crm(tenant_id: int, days: int = 7) -> str:
    """Пробная выгрузка: сколько лидов и сделок реально приходит из CRM.
    Пишет результат в журнал запусков (виден в панели)."""
    adapter = build_crm_adapter(tenant_id)
    if adapter is None:
        return "CRM не подключена — заполните интеграцию в карточке клиента"

    run_id = db.start_run(tenant_id, "crm_sync")
    try:
        since = datetime.now() - timedelta(days=days)
        leads = adapter.get_leads(since)
        deals = adapter.get_deals(since)
        won = sum(1 for d in deals if d.unified_stage == "WON")
        sample = leads[0] if leads else None
        report = (f"Синхронизация CRM ({adapter.kind}) за {days} дн.:\n"
                  f"лидов: {len(leads)}, сделок: {len(deals)} (из них выиграно: {won})\n")
        if sample:
            report += (f"пример лида: id={sample.external_id}, "
                       f"тел={sample.phone_e164 or '—'}, статус={sample.unified_status}")
        db.finish_run(run_id, "done", report)

        integ = db.get_integration(tenant_id, adapter.kind)
        if integ:
            db.set_integration_status(integ["id"], "active",
                                      f"последняя синх.: {len(leads)} лидов, {len(deals)} сделок")
        return report
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        integ = db.get_integration(tenant_id, adapter.kind)
        if integ:
            db.set_integration_status(integ["id"], "error", str(e))
        raise
