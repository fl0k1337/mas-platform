"""
Контролёр лидов. Работает в двух режимах:

  • CRM подключена (Битрикс24 и т.п.) — РЕАЛЬНАЯ проверка: находит лиды,
    зависшие в статусе NEW дольше норматива (отдел продаж не взял в работу),
    и лиды без телефона. Это половина ежедневной ручной задачи владельца
    («проверить, что ОП отработал лиды») — уже автоматизирована.
  • CRM не подключена — демо-режим на тестовых данных (как раньше).

Полная сверка Calltouch↔CRM («дошёл ли лид из рекламы до CRM») добавится,
когда будет подключён адаптер Calltouch. Здесь для неё оставлено место.
Всё детерминированно, без LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import db, tg
from app.integrations.service import build_crm_adapter

SLA_HOURS = 4          # за сколько часов ОП обязан взять лид в работу
LOOKBACK_DAYS = 7      # за какой период смотрим лиды


# ----------------------------------------------------------------------------
# РЕАЛЬНЫЙ режим: анализ лидов из подключённой CRM
# ----------------------------------------------------------------------------

def _analyze_real(tenant: dict, adapter) -> str:
    now = datetime.now()
    since = now - timedelta(days=LOOKBACK_DAYS)
    leads = adapter.get_leads(since)

    overdue, no_phone, ok = [], [], 0
    sla = timedelta(hours=SLA_HOURS)
    for lead in leads:
        # даты из CRM приходят с часовым поясом — сравниваем без tz (один регион)
        created = lead.created_at.replace(tzinfo=None)
        if lead.phone_e164 is None:
            no_phone.append(lead)
        if lead.unified_status == "NEW" and (now - created) > sla:
            overdue.append(lead)
        elif lead.unified_status in ("IN_PROGRESS", "QUALIFIED"):
            ok += 1

    lines = [f"🔎 Контроль лидов (по CRM) — {tenant['name']} за {LOOKBACK_DAYS} дн.",
             f"Всего лидов: {len(leads)}",
             f"✅ в работе/квалифицировано: {ok}",
             f"⏰ НЕ взято в работу дольше {SLA_HOURS} ч: {len(overdue)}"]
    for l in overdue[:15]:
        created = l.created_at.replace(tzinfo=None)
        lines.append(f"   • лид {l.external_id}, тел {l.phone_e164 or '—'}, "
                     f"отв. {l.responsible or '—'}, создан {created:%d.%m %H:%M}")
    if no_phone:
        lines.append(f"❓ лидов без телефона: {len(no_phone)} "
                     f"(id: {', '.join(l.external_id for l in no_phone[:10])})")
    lines.append("")
    lines.append("Примечание: полная сверка с Calltouch (дошёл ли лид из рекламы "
                 "до CRM) появится после подключения Calltouch.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# ДЕМО режим: тестовые данные (когда CRM не подключена)
# ----------------------------------------------------------------------------

def _analyze_demo(tenant: dict) -> str:
    return (f"🔎 Контроль лидов (ДЕМО, CRM не подключена) — {tenant['name']}\n"
            f"Подключите CRM в карточке клиента, чтобы проверять реальные лиды.\n"
            f"Пример того, что увидите: «⏰ 2 лида не взяты в работу дольше "
            f"{SLA_HOURS} ч; ❓ 1 лид без телефона».")


# ----------------------------------------------------------------------------

def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    run_id = db.start_run(tenant_id, "lead_control")
    try:
        adapter = build_crm_adapter(tenant_id)
        if adapter is not None:
            report = _analyze_real(tenant, adapter)
            mode = "реальные данные CRM"
        else:
            report = _analyze_demo(tenant)
            mode = "демо"
        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"контроль лидов выполнен ({mode})"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
