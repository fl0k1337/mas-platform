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
from app.integrations.service import build_calltouch_adapter, build_crm_adapter

SLA_HOURS = 4          # за сколько часов ОП обязан взять лид в работу
LOOKBACK_DAYS = 7      # за какой период смотрим лиды
MATCH_WINDOW_HOURS = 24  # окно поиска лида в CRM вокруг звонка


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
# ПОЛНАЯ СВЕРКА: Calltouch (звонки из рекламы) ↔ CRM (лиды)
# ----------------------------------------------------------------------------

def _reconcile(tenant: dict, crm_adapter, ct_adapter) -> str:
    now = datetime.now()
    since = now - timedelta(days=LOOKBACK_DAYS)
    leads = crm_adapter.get_leads(since)
    calls = ct_adapter.get_calls(since)

    # индекс лидов CRM по телефону
    by_phone: dict[str, list] = {}
    for lead in leads:
        if lead.phone_e164:
            by_phone.setdefault(lead.phone_e164, []).append(lead)

    window = timedelta(hours=MATCH_WINDOW_HOURS)
    sla = timedelta(hours=SLA_HOURS)
    missing, not_processed, unmatchable, ok = [], [], [], 0

    for call in calls:
        if not call.is_target:
            continue                      # нецелевые звонки не сверяем
        if call.phone_e164 is None:
            unmatchable.append(call)
            continue
        call_time = call.occurred_at.replace(tzinfo=None)
        cands = [l for l in by_phone.get(call.phone_e164, [])
                 if abs(l.created_at.replace(tzinfo=None) - call_time) <= window]
        if not cands:
            missing.append(call)          # звонок был, лида в CRM нет — потеря!
            continue
        lead = min(cands, key=lambda l: abs(l.created_at.replace(tzinfo=None) - call_time))
        if lead.unified_status == "NEW" and (now - lead.created_at.replace(tzinfo=None)) > sla:
            not_processed.append((call, lead))
        else:
            ok += 1

    target_calls = sum(1 for c in calls if c.is_target)
    lines = [f"🔎 Полная сверка Calltouch↔CRM — {tenant['name']} за {LOOKBACK_DAYS} дн.",
             f"Целевых звонков: {target_calls}, лидов в CRM: {len(leads)}",
             f"✅ дошли и обработаны: {ok}",
             f"🚨 звонок был — лида в CRM НЕТ: {len(missing)}",
             f"⏰ лид есть, но не взят в работу за {SLA_HOURS} ч: {len(not_processed)}",
             f"❓ несверяемые (скрытый номер): {len(unmatchable)}"]
    if missing:
        lines.append("\nПОТЕРЯННЫЕ ЛИДЫ (звонок без карточки в CRM):")
        for c in missing[:15]:
            lines.append(f"   🚨 {c.phone_e164}, звонок {c.occurred_at:%d.%m %H:%M}"
                         f"{', ' + c.source if c.source else ''}")
    if not_processed:
        lines.append("\nНЕ ВЗЯТЫ В РАБОТУ:")
        for c, l in not_processed[:15]:
            lines.append(f"   ⏰ лид {l.external_id}, {c.phone_e164}, отв. {l.responsible or '—'}")
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
        crm = build_crm_adapter(tenant_id)
        ct = build_calltouch_adapter(tenant_id)
        if crm is not None and ct is not None:
            report = _reconcile(tenant, crm, ct)      # полная сверка
            mode = "полная сверка Calltouch↔CRM"
        elif crm is not None:
            report = _analyze_real(tenant, crm)        # только CRM (SLA)
            mode = "по CRM (без Calltouch)"
        else:
            report = _analyze_demo(tenant)
            mode = "демо"
        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"контроль лидов выполнен ({mode})"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
