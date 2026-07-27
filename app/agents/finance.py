"""
Финансовый контролёр. Два режима (как у leads.py):

  • CRM подключена — РЕАЛЬНЫЙ отчёт по продажам: берёт закрытые (WON) сделки
    из CRM за период, считает выручку, средний чек, разбивку по менеджерам,
    подсвечивает сделки без суммы и «зависшие» дорогие сделки в работе.
  • CRM не подключена — демо на тестовых данных.

Сверка «сметы ↔ сделки» (сопоставление плановых сумм с фактическими) добавится,
когда подключим источник смет (Google Sheets клиента). Сейчас фокус на факте
из CRM — это то, что владелец сегодня сводит руками.

Расчёты — детерминированные (без LLM). LLM (если доступна) добавляет короткую
пояснительную записку «на что обратить внимание».
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from app import db, tg
from app.integrations.service import build_crm_adapter
from app.llm import build_llm

LOOKBACK_DAYS = 7
BIG_DEAL = 100_000          # порог «крупной» сделки для подсветки зависших

SYSTEM_PROMPT = """\
Ты — финансовый аналитик компании ({industry}). Тебе передают сводку по продажам
из CRM за неделю. Напиши короткую записку руководителю (до 8 строк): на что
обратить внимание, где возможные потери (сделки без суммы, крупные зависшие).
Только по переданным цифрам, ничего не выдумывай. Русский язык.
"""


def _analyze_real(tenant: dict, adapter) -> str:
    now = datetime.now()
    since = now - timedelta(days=LOOKBACK_DAYS)
    deals = adapter.get_deals(since)

    won = [d for d in deals if d.unified_stage == "WON"]
    lost = [d for d in deals if d.unified_stage == "LOST"]
    in_work = [d for d in deals if d.unified_stage not in ("WON", "LOST")]

    revenue = sum(d.amount or 0 for d in won)
    avg_check = revenue / len(won) if won else 0
    no_amount = [d for d in won if not d.amount]
    big_stuck = [d for d in in_work if (d.amount or 0) >= BIG_DEAL]

    by_manager: dict[str, dict] = defaultdict(lambda: {"count": 0, "sum": 0.0})
    for d in won:
        m = by_manager[d.responsible or "—"]
        m["count"] += 1
        m["sum"] += d.amount or 0

    lines = [f"💰 Отчёт по продажам (по CRM) — {tenant['name']} за {LOOKBACK_DAYS} дн.",
             f"Выиграно сделок: {len(won)} на {revenue:,.0f} ₽".replace(",", " "),
             f"Средний чек: {avg_check:,.0f} ₽".replace(",", " "),
             f"В работе: {len(in_work)}, проиграно: {len(lost)}", "",
             "По менеджерам (выигранные):"]
    for mgr, s in sorted(by_manager.items(), key=lambda x: -x[1]["sum"]):
        lines.append(f"   • {mgr}: {s['count']} сделок на "
                     f"{s['sum']:,.0f} ₽".replace(",", " "))

    if no_amount:
        lines.append(f"\n⚠ Выигранные сделки БЕЗ суммы: {len(no_amount)} "
                     f"(id: {', '.join(d.external_id for d in no_amount[:10])}) — проверить")
    if big_stuck:
        lines.append(f"⏰ Крупные сделки (≥{BIG_DEAL:,.0f} ₽) зависли в работе: "
                     f"{len(big_stuck)}".replace(",", " "))
        for d in big_stuck[:8]:
            lines.append(f"   • сделка {d.external_id}, {d.amount:,.0f} ₽, "
                         f"отв. {d.responsible or '—'}".replace(",", " "))

    report = "\n".join(lines)

    llm = build_llm(temperature=0.2)
    if llm is not None and (no_amount or big_stuck):
        note = llm.invoke([
            ("system", SYSTEM_PROMPT.format(industry=tenant["industry"])),
            ("user", report),
        ]).content
        report += "\n\n📋 " + note

    return report


def _analyze_demo(tenant: dict) -> str:
    return (f"💰 Финотчёт (ДЕМО, CRM не подключена) — {tenant['name']}\n"
            f"Подключите CRM в карточке клиента, чтобы видеть реальные продажи: "
            f"выручку, средний чек, разбивку по менеджерам и зависшие сделки.")


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    run_id = db.start_run(tenant_id, "finance_check")
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
        return f"финотчёт готов ({mode})"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
