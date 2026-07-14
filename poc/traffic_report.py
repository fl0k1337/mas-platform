"""
Proof of Concept: агент еженедельного отчёта по рекламному трафику.
Стек: FastAPI + LangGraph.

Демонстрирует ключевые паттерны ядра платформы:
  1. Приём задачи через REST (в проде задачу кладёт Celery Beat, а не человек).
  2. Детерминированные шаги (сбор данных, поиск аномалий) — обычный код без LLM.
  3. LLM подключается только на шаге интерпретации и написания отчёта.
  4. Типизированное состояние графа (TypedDict + Pydantic), а не свободный текст.
  5. Точка расширения для чекпоинтов/согласования человеком (закомментирована).

Запуск:
    pip install fastapi uvicorn langgraph langchain-openai pydantic
    export OPENAI_API_KEY=sk-...          # либо см. build_llm() для Anthropic
    uvicorn traffic_report_poc:app --reload

    curl -X POST localhost:8000/agents/traffic-report \
         -H 'Content-Type: application/json' \
         -d '{"tenant_id": "demo", "period": "2026-W28"}'

Без ключа LLM PoC тоже работает: шаг интерпретации вернёт заглушку,
что удобно для отладки детерминированной части.
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv
load_dotenv()  # подхватывает TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID из файла .env

# --------------------------------------------------------------------------
# 1. МОДЕЛИ ДАННЫХ (в проде — слой Integration Layer, unified-модели)
# --------------------------------------------------------------------------

class ChannelMetrics(BaseModel):
    """Агрегат по рекламному каналу за период (из Calltouch + CRM)."""
    channel: str
    calls: int
    target_calls: int
    requests: int          # заявки с форм
    leads_in_crm: int
    deals_won: int
    revenue: float
    ad_spend: float

    @property
    def cpl(self) -> float | None:
        total_leads = self.leads_in_crm
        return round(self.ad_spend / total_leads, 2) if total_leads else None


class Anomaly(BaseModel):
    channel: str
    metric: str
    current: float
    baseline: float
    deviation_pct: float
    severity: str  # info | warning | critical


class TrafficReportRequest(BaseModel):
    tenant_id: str
    period: str  # напр. "2026-W28"


# --------------------------------------------------------------------------
# 2. ЗАГЛУШКИ КЛИЕНТОВ ВНЕШНИХ API
#    В проде: CalltouchClient и CRMAdapter из Integration Layer,
#    данные уже лежат в PostgreSQL (воркеры выгрузили заранее).
# --------------------------------------------------------------------------

def fetch_current_metrics(tenant_id: str, period: str) -> list[ChannelMetrics]:
    """MOCK: агрегаты текущей недели (в проде — SQL по crm_leads/ct_interactions)."""
    return [
        ChannelMetrics(channel="yandex_direct", calls=142, target_calls=98,
                       requests=61, leads_in_crm=155, deals_won=17,
                       revenue=2_140_000, ad_spend=310_000),
        ChannelMetrics(channel="google_ads", calls=38, target_calls=21,
                       requests=19, leads_in_crm=39, deals_won=3,
                       revenue=390_000, ad_spend=95_000),
        ChannelMetrics(channel="2gis", calls=57, target_calls=44,
                       requests=4, leads_in_crm=48, deals_won=9,
                       revenue=760_000, ad_spend=40_000),
        # Аномалия: звонки упали втрое к базовой линии
        ChannelMetrics(channel="yandex_maps", calls=12, target_calls=7,
                       requests=2, leads_in_crm=11, deals_won=1,
                       revenue=95_000, ad_spend=35_000),
    ]


def fetch_baseline_metrics(tenant_id: str, period: str) -> dict[str, dict[str, float]]:
    """MOCK: медианы за предыдущие 4 недели, по каналам."""
    return {
        "yandex_direct": {"calls": 131, "leads_in_crm": 149, "cpl": 2005.0},
        "google_ads":    {"calls": 41,  "leads_in_crm": 44,  "cpl": 2380.0},
        "2gis":          {"calls": 52,  "leads_in_crm": 45,  "cpl": 870.0},
        "yandex_maps":   {"calls": 36,  "leads_in_crm": 33,  "cpl": 1030.0},
    }


# --------------------------------------------------------------------------
# 3. СОСТОЯНИЕ ГРАФА LANGGRAPH
# --------------------------------------------------------------------------

class ReportState(TypedDict):
    tenant_id: str
    period: str
    metrics: list[dict[str, Any]]          # сериализованные ChannelMetrics
    baseline: dict[str, dict[str, float]]
    anomalies: list[dict[str, Any]]        # сериализованные Anomaly
    report_md: str                          # итоговый отчёт (markdown)
    telegram_status: str                    # результат отправки в Telegram


# --------------------------------------------------------------------------
# 4. УЗЛЫ ГРАФА
# --------------------------------------------------------------------------

def node_collect_data(state: ReportState) -> dict:
    """Детерминированный шаг: собрать метрики. Без LLM."""
    metrics = fetch_current_metrics(state["tenant_id"], state["period"])
    baseline = fetch_baseline_metrics(state["tenant_id"], state["period"])
    return {
        "metrics": [m.model_dump() for m in metrics],
        "baseline": baseline,
    }


def node_detect_anomalies(state: ReportState) -> dict:
    """Детерминированный шаг: пороговые правила, без LLM.

    Правило PoC: отклонение метрики от медианы 4 недель более чем
    на 30% -> warning, более чем на 60% -> critical.
    """
    anomalies: list[Anomaly] = []
    for m_raw in state["metrics"]:
        m = ChannelMetrics(**m_raw)
        base = state["baseline"].get(m.channel, {})
        checks = {"calls": float(m.calls), "leads_in_crm": float(m.leads_in_crm)}
        if m.cpl is not None and "cpl" in base:
            checks["cpl"] = m.cpl
        for metric, current in checks.items():
            baseline_val = base.get(metric)
            if not baseline_val:
                continue
            deviation = (current - baseline_val) / baseline_val * 100
            if abs(deviation) >= 30:
                anomalies.append(Anomaly(
                    channel=m.channel, metric=metric,
                    current=current, baseline=baseline_val,
                    deviation_pct=round(deviation, 1),
                    severity="critical" if abs(deviation) >= 60 else "warning",
                ))
    return {"anomalies": [a.model_dump() for a in anomalies]}


OLLAMA_MODEL = "qwen2.5:7b"  # модель, которую вы скачали на шаге 3

def build_llm():
    """Локальная бесплатная модель через Ollama.
    Если Ollama не запущена — вернём None, и скрипт отработает в черновом режиме."""
    try:
        import httpx
        httpx.get("http://localhost:11434/api/tags", timeout=2)
    except Exception:
        return None  # Ollama выключена — работаем без LLM
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=OLLAMA_MODEL,
        base_url="http://localhost:11434/v1",  # Ollama притворяется OpenAI — код менять не нужно
        api_key="ollama",                      # заглушка, локально ключ не проверяется
        temperature=0.2,
    )

ANALYST_SYSTEM_PROMPT = """\
Ты — маркетинговый аналитик. Тебе передают агрегированные показатели
рекламного трафика по каналам за неделю и список алгоритмически найденных
аномалий (отклонения от медианы предыдущих 4 недель).

Задача:
1. Краткое резюме недели (до 5 строк).
2. Интерпретация каждой аномалии: 1-2 правдоподобные гипотезы причин.
3. 3-5 конкретных рекомендаций, отсортированных по важности.

Правила: используй ТОЛЬКО переданные цифры, ничего не выдумывай.
Если данных недостаточно для вывода — прямо скажи об этом.
Ответ — в Markdown, на русском языке.
"""


def node_write_report(state: ReportState) -> dict:
    """Единственный LLM-шаг: интерпретация и текст отчёта."""
    metrics_table = "\n".join(
        f"- {m['channel']}: звонки={m['calls']} (целевых {m['target_calls']}), "
        f"заявки={m['requests']}, лиды CRM={m['leads_in_crm']}, "
        f"сделки={m['deals_won']}, выручка={m['revenue']:.0f} ₽, "
        f"расход={m['ad_spend']:.0f} ₽"
        for m in state["metrics"]
    )
    anomalies_block = "\n".join(
        f"- [{a['severity']}] {a['channel']} / {a['metric']}: "
        f"{a['current']} против базовой {a['baseline']} ({a['deviation_pct']:+.1f}%)"
        for a in state["anomalies"]
    ) or "Аномалий не обнаружено."

    user_msg = (
        f"Период: {state['period']}\n\nМетрики по каналам:\n{metrics_table}\n\n"
        f"Найденные аномалии:\n{anomalies_block}"
    )

    llm = build_llm()
    if llm is None:  # оффлайн-режим для отладки детерминированной части
        report = (f"# Отчёт по трафику {state['period']} (LLM недоступен — черновик)\n\n"
                  f"## Метрики\n{metrics_table}\n\n## Аномалии\n{anomalies_block}\n")
    else:
        resp = llm.invoke([("system", ANALYST_SYSTEM_PROMPT), ("user", user_msg)])
        report = resp.content

    # В проде здесь: сохранить в generated_content, отправить в Google Sheets,
    # уведомить ответственного в Telegram/MAX.
    return {"report_md": report}

TG_API = "https://api.telegram.org"


def node_notify_telegram(state: ReportState) -> dict:
    """Отправка готового отчёта в Telegram. Детерминированный шаг, без LLM."""
    import httpx

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"telegram_status": "skipped: заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env"}

    text = f"📊 Отчёт по трафику {state['period']}\n\n{state['report_md']}"
    try:
        # Лимит Telegram — 4096 символов на сообщение, поэтому режем длинный отчёт на части
        for i in range(0, len(text), 4000):
            resp = httpx.post(
                f"{TG_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[i:i + 4000]},
                timeout=15,
            )
            resp.raise_for_status()
        return {"telegram_status": "sent"}
    except Exception as e:
        return {"telegram_status": f"error: {e}"}

# --------------------------------------------------------------------------
# 5. СБОРКА ГРАФА
# --------------------------------------------------------------------------

from langgraph.graph import END, START, StateGraph  # noqa: E402


def build_graph():
    g = StateGraph(ReportState)
    g.add_node("collect_data", node_collect_data)
    g.add_node("detect_anomalies", node_detect_anomalies)
    g.add_node("write_report", node_write_report)
    g.add_node("notify_telegram", node_notify_telegram)

    g.add_edge(START, "collect_data")
    g.add_edge("collect_data", "detect_anomalies")
    g.add_edge("detect_anomalies", "write_report")
    g.add_edge("write_report", "notify_telegram")
    g.add_edge("notify_telegram", END)

    # Прод-версия: чекпоинты в PostgreSQL + пауза на согласование человеком:
    #   from langgraph.checkpoint.postgres import PostgresSaver
    #   return g.compile(checkpointer=PostgresSaver(...),
    #                    interrupt_before=["publish"])
    return g.compile()


GRAPH = build_graph()

# --------------------------------------------------------------------------
# 6. FASTAPI
# --------------------------------------------------------------------------

app = FastAPI(title="MAS Platform PoC", version="0.1.0")


@app.post("/agents/traffic-report")
def run_traffic_report(req: TrafficReportRequest) -> dict:
    """В проде этот запуск инициирует Celery Beat по расписанию тенанта,
    а эндпоинт остаётся для ручного перезапуска из UI."""
    initial: ReportState = {
        "tenant_id": req.tenant_id, "period": req.period,
        "metrics": [], "baseline": {}, "anomalies": [], "report_md": "",
        "telegram_status": "",
    }
    final = GRAPH.invoke(initial)
    return {
        "tenant_id": req.tenant_id,
        "period": req.period,
        "anomalies": final["anomalies"],
        "report_markdown": final["report_md"],
        "telegram": final["telegram_status"],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    # Быстрый прогон без веб-сервера: python traffic_report_poc.py
    result = run_traffic_report(TrafficReportRequest(tenant_id="demo", period="2026-W28"))
    print(result["report_markdown"])