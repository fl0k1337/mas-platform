"""
Аналитик трафика. Два режима:

  • Calltouch подключён — РЕАЛЬНЫЕ данные: звонки за период группируются по
    источникам (utm_source), считаются целевые/уникальные, конверсия в лиды CRM
    (если CRM тоже подключена), сравнение с предыдущим периодом и поиск аномалий.
  • Не подключён — демо на тестовых цифрах.

Цифры и аномалии считает КОД (детерминированно), нейросеть только объясняет
и даёт рекомендации — по принципу «честной автоматизации» из спецификации.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from app import db, tg
from app.integrations.service import build_calltouch_adapter, build_crm_adapter
from app.llm import build_llm

PERIOD_DAYS = 7
ANOMALY_PCT = 30          # отклонение от прошлого периода, с которого это аномалия

SYSTEM_PROMPT = """\
Ты — маркетинговый аналитик компании ({industry}).
Тебе передают показатели рекламного трафика по источникам за период, сравнение
с прошлым периодом и список алгоритмически найденных аномалий.

Задача: (1) краткое резюме до 4 строк, (2) 1-2 гипотезы по каждой аномалии,
(3) 3-5 практических рекомендаций по важности.
Используй ТОЛЬКО переданные цифры, ничего не выдумывай. Русский язык.
"""


def _group_by_source(calls: list) -> dict[str, dict]:
    """Звонки -> сводка по источникам."""
    out: dict[str, dict] = defaultdict(lambda: {"calls": 0, "target": 0, "unique": 0})
    for c in calls:
        s = out[c.source or "не определён"]
        s["calls"] += 1
        s["target"] += 1 if c.is_target else 0
        s["unique"] += 1 if c.is_unique else 0
    return dict(out)


def _find_anomalies(now_stats: dict, prev_stats: dict) -> list[str]:
    """Детерминированный поиск отклонений по источникам."""
    found = []
    for src, cur in now_stats.items():
        prev = prev_stats.get(src)
        if not prev or not prev["calls"]:
            if cur["calls"] >= 5:
                found.append(f"{src}: новый источник, {cur['calls']} звонков "
                             f"(в прошлом периоде не было)")
            continue
        dev = (cur["calls"] - prev["calls"]) / prev["calls"] * 100
        if abs(dev) >= ANOMALY_PCT:
            found.append(f"{src}: звонков {cur['calls']} против {prev['calls']} "
                         f"({dev:+.0f}%)")
    for src, prev in prev_stats.items():
        if src not in now_stats and prev["calls"] >= 5:
            found.append(f"{src}: звонки прекратились (было {prev['calls']})")
    return found


def _analyze_real(tenant: dict, ct, crm) -> str:
    now = datetime.now()
    since = now - timedelta(days=PERIOD_DAYS)
    prev_since = now - timedelta(days=PERIOD_DAYS * 2)

    all_calls = ct.get_calls(prev_since)
    cur_calls = [c for c in all_calls if c.occurred_at >= since]
    prev_calls = [c for c in all_calls if c.occurred_at < since]

    cur_stats = _group_by_source(cur_calls)
    prev_stats = _group_by_source(prev_calls)
    anomalies = _find_anomalies(cur_stats, prev_stats)

    total, target = len(cur_calls), sum(1 for c in cur_calls if c.is_target)
    prev_total = len(prev_calls)
    delta = f"{(total - prev_total) / prev_total * 100:+.0f}%" if prev_total else "—"

    lines = [f"📊 Отчёт по трафику — {tenant['name']} за {PERIOD_DAYS} дн.",
             f"Звонков: {total} (целевых {target}), к прошлому периоду: {delta}", ""]

    if crm is not None:
        leads = crm.get_leads(since)
        conv = f"{len(leads) / target * 100:.0f}%" if target else "—"
        lines.append(f"Лидов в CRM: {len(leads)} · конверсия из целевых звонков: {conv}")
        lines.append("")

    lines.append("По источникам:")
    for src, s in sorted(cur_stats.items(), key=lambda x: -x[1]["calls"]):
        was = prev_stats.get(src, {}).get("calls", 0)
        lines.append(f"   • {src}: {s['calls']} звонков "
                     f"(целевых {s['target']}, уникальных {s['unique']}; было {was})")
    if not cur_stats:
        lines.append("   звонков за период не было")

    lines.append("")
    lines.append(f"Аномалии (отклонение ≥{ANOMALY_PCT}%):")
    lines += [f"   ⚠ {a}" for a in anomalies] or ["   не обнаружены"]

    report = "\n".join(lines)
    llm = build_llm(temperature=0.2)
    if llm is not None and anomalies:
        report += "\n\n📋 " + llm.invoke([
            ("system", SYSTEM_PROMPT.format(industry=tenant["industry"])),
            ("user", report)]).content
    return report


def _analyze_demo(tenant: dict) -> str:
    return (f"📊 Отчёт по трафику (ДЕМО, Calltouch не подключён) — {tenant['name']}\n"
            f"Подключите Calltouch в карточке клиента, чтобы видеть реальные звонки "
            f"по источникам, конверсию в лиды и аномалии.\n\n"
            f"Пример: «yandex_direct: 142 звонка (целевых 98), было 131»; "
            f"«⚠ yandex_maps: звонков 12 против 36 (−67%)».")


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    run_id = db.start_run(tenant_id, "traffic_report")
    try:
        ct = build_calltouch_adapter(tenant_id)
        if ct is not None:
            report = _analyze_real(tenant, ct, build_crm_adapter(tenant_id))
            mode = "реальные данные Calltouch"
        else:
            report = _analyze_demo(tenant)
            mode = "демо"
        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"отчёт по трафику готов ({mode})"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
