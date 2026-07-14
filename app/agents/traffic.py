"""Агент-Аналитик трафика. Данные пока тестовые (mock) — при подключении
реального Calltouch/CRM заменяются только функции fetch_*."""

from __future__ import annotations

from app import db, tg
from app.llm import build_llm

SYSTEM_PROMPT = """\
Ты — маркетинговый аналитик компании ({industry}).
Тебе передают показатели рекламного трафика по каналам за неделю и список
алгоритмически найденных аномалий. Задача: короткое резюме (до 5 строк),
1-2 гипотезы по каждой аномалии, 3-5 рекомендаций по важности.
Используй только переданные цифры, ничего не выдумывай. Ответ на русском, Markdown.
"""


def fetch_current_metrics() -> list[dict]:
    return [
        {"channel": "yandex_direct", "calls": 142, "leads": 155, "spend": 310_000},
        {"channel": "google_ads", "calls": 38, "leads": 39, "spend": 95_000},
        {"channel": "2gis", "calls": 57, "leads": 48, "spend": 40_000},
        {"channel": "yandex_maps", "calls": 12, "leads": 11, "spend": 35_000},
    ]


def fetch_baseline() -> dict[str, dict]:
    return {"yandex_direct": {"calls": 131, "leads": 149},
            "google_ads": {"calls": 41, "leads": 44},
            "2gis": {"calls": 52, "leads": 45},
            "yandex_maps": {"calls": 36, "leads": 33}}


def detect_anomalies(metrics: list[dict], baseline: dict) -> list[str]:
    """Детерминированный поиск отклонений >30% от медианы прошлых недель."""
    found = []
    for m in metrics:
        base = baseline.get(m["channel"], {})
        for key in ("calls", "leads"):
            if base.get(key):
                dev = (m[key] - base[key]) / base[key] * 100
                if abs(dev) >= 30:
                    found.append(f"{m['channel']}/{key}: {m[key]} против {base[key]} ({dev:+.0f}%)")
    return found


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    run_id = db.start_run(tenant_id, "traffic_report")
    try:
        metrics = fetch_current_metrics()
        anomalies = detect_anomalies(metrics, fetch_baseline())

        table = "\n".join(f"- {m['channel']}: звонки={m['calls']}, лиды={m['leads']}, "
                          f"расход={m['spend']:.0f} ₽" for m in metrics)
        anom = "\n".join(f"- {a}" for a in anomalies) or "аномалий нет"

        llm = build_llm(temperature=0.2)
        if llm is None:
            report = f"# Отчёт (без LLM)\n\n{table}\n\nАномалии:\n{anom}"
        else:
            report = llm.invoke([
                ("system", SYSTEM_PROMPT.format(industry=tenant["industry"])),
                ("user", f"Метрики:\n{table}\n\nАномалии:\n{anom}"),
            ]).content

        tg.notify(f"📊 Отчёт по трафику — {tenant['name']}\n\n{report}")
        db.finish_run(run_id, "done", report)
        return f"отчёт готов, аномалий: {len(anomalies)}"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
