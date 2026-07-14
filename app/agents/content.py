"""
Агент-Копирайтер (граф LangGraph): контент недели для тенанта.

Отличия от прототипа content_weekly.py:
  - профиль бренда и контент-план читаются из БД (таблицы tenants, content_plan);
  - результаты пишутся в БД (generated_content) со статусом pending_approval;
  - согласование — в веб-панели (или Telegram-ботом, механика та же).
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app import db
from app.llm import build_llm

CHANNEL_LIMITS = {"telegram": 3500, "instagram": 2200, "max": 3500}
MAX_ATTEMPTS = 2

SYSTEM_PROMPT = """\
Ты — SMM-копирайтер бренда «{name}» ({industry}).
Tone of voice: {tone}.

Правила (нарушение = брак):
1. Пост пишется ПОД КОНКРЕТНЫЙ медиафайл — текст должен обыгрывать то, что на нём видно.
2. Обязателен призыв к действию (например: {cta_examples}).
3. ЗАПРЕЩЕНЫ слова и фразы: {stop_words}.
4. Не выдумывай цены, даты и факты, которых нет в задании.
5. Длина — не больше {limit} символов, это канал {channel}.
6. В ответе — ТОЛЬКО готовый текст поста, без пояснений и вариантов.
"""


class ContentState(TypedDict):
    tenant: dict[str, Any]
    plan: list[dict[str, Any]]
    idx: int
    attempts: int
    draft: str
    feedback: str
    valid: bool
    problems: list[str]
    results: list[dict[str, Any]]


def node_next_item(state: ContentState) -> dict:
    return {"idx": state["idx"] + 1, "attempts": 0, "draft": "",
            "feedback": "", "valid": False, "problems": []}


def node_generate(state: ContentState) -> dict:
    item = state["plan"][state["idx"]]
    brand = state["tenant"]["brand_profile"]
    limit = CHANNEL_LIMITS.get(item["channel"], 3500)

    llm = build_llm(temperature=0.7)
    if llm is None:
        draft = (f"[Черновик без LLM] {item['theme']}. Подробности на фото! "
                 f"Запишитесь — подберём удобное время. Ждём вас!")
    else:
        system = SYSTEM_PROMPT.format(
            name=state["tenant"]["name"], industry=state["tenant"]["industry"],
            tone=brand.get("tone", "нейтрально, дружелюбно"),
            cta_examples=", ".join(brand.get("cta_words", ["запишитесь"])[:4]),
            stop_words="; ".join(brand.get("stop_words", [])) or "нет",
            limit=limit, channel=item["channel"],
        )
        user = (f"Тема поста: {item['theme']}\n"
                f"Медиафайл, под который пишем: {item['media']}")
        if state["feedback"]:
            user += (f"\n\nЗамечания редактора к прошлой версии:\n{state['feedback']}\n"
                     f"Прошлая версия:\n{state['draft']}\n\nПерепиши с учётом замечаний.")
        draft = llm.invoke([("system", system), ("user", user)]).content.strip()

    return {"draft": draft, "attempts": state["attempts"] + 1}


def node_validate(state: ContentState) -> dict:
    item = state["plan"][state["idx"]]
    brand = state["tenant"]["brand_profile"]
    limit = CHANNEL_LIMITS.get(item["channel"], 3500)
    text, text_low = state["draft"], state["draft"].lower()
    problems: list[str] = []

    if len(text) > limit:
        problems.append(f"длина {len(text)} > лимита {limit}")
    if len(text) < 100:
        problems.append("короче 100 символов")
    for sw in brand.get("stop_words", []):
        if sw.lower() in text_low:
            problems.append(f"запрещённая фраза «{sw}»")
    cta_words = brand.get("cta_words", [])
    if cta_words and not any(c in text_low for c in cta_words):
        problems.append("нет призыва к действию")

    return {"valid": not problems, "problems": problems, "feedback": "; ".join(problems)}


def node_record(state: ContentState) -> dict:
    item = state["plan"][state["idx"]]
    result = {"theme": item["theme"], "channel": item["channel"], "text": state["draft"],
              "status": "pending_approval" if state["valid"] else "needs_human",
              "attempts": state["attempts"], "problems": state["problems"]}
    return {"results": state["results"] + [result]}


def route_after_validate(state: ContentState) -> str:
    if not state["valid"] and state["attempts"] < MAX_ATTEMPTS:
        return "revise"
    return "record"


def route_after_record(state: ContentState) -> str:
    return "next_item" if state["idx"] + 1 < len(state["plan"]) else "finish"


def build_graph():
    g = StateGraph(ContentState)
    g.add_node("next_item", node_next_item)
    g.add_node("generate", node_generate)
    g.add_node("validate", node_validate)
    g.add_node("record", node_record)
    g.add_edge(START, "next_item")
    g.add_edge("next_item", "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            {"revise": "generate", "record": "record"})
    g.add_conditional_edges("record", route_after_record,
                            {"next_item": "next_item", "finish": END})
    return g.compile()


def run(tenant_id: int) -> str:
    """Точка входа: запускается из веб-панели или планировщика."""
    tenant = db.get_tenant(tenant_id)
    plan = db.get_plan(tenant_id)
    if not tenant or not plan:
        return "нет тенанта или пустой контент-план"

    run_id = db.start_run(tenant_id, "content_weekly")
    try:
        initial: ContentState = {
            "tenant": tenant, "plan": plan, "idx": -1, "attempts": 0,
            "draft": "", "feedback": "", "valid": False, "problems": [], "results": [],
        }
        final = build_graph().invoke(initial)
        for r in final["results"]:
            db.save_content(tenant_id, run_id, r["channel"], r["theme"], r["text"],
                            r["status"], r["problems"], r["attempts"])
        ok = sum(r["status"] == "pending_approval" for r in final["results"])
        summary = (f"готово {len(final['results'])} черновиков, "
                   f"{ok} прошли автопроверку — ждут согласования в панели")
        db.finish_run(run_id, "done", summary)
        return summary
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
