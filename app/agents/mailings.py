"""
Агент рассылок: тексты SMS и WhatsApp/MAX + план отправки на неделю.

Переиспользует механику Копирайтера (граф с циклом доработки), но:
  - темы берёт из того же контент-плана клиента (акции недели = темы рассылок);
  - для каждой темы делает ДВЕ версии: SMS (жёсткий лимит длины, без эмодзи)
    и WhatsApp/MAX (разговорный тон);
  - раскладывает отправки по будням недели (детерминированно);
  - черновики попадают в общую очередь согласования в панели.

Важно про SMS: кириллическая SMS — это 70 символов на сегмент; лимит ниже
выставлен в 2 сегмента (134 символа), чтобы рассылка не разоряла клиента.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app import db
from app.llm import build_llm

SMS_LIMIT = 134        # 2 сегмента кириллицей
WA_LIMIT = 600
MAX_ATTEMPTS = 2
SEND_SLOTS = ["понедельник 12:00", "вторник 12:00", "среда 12:00",
              "четверг 12:00", "пятница 12:00"]

PROMPTS = {
    "sms": """\
Ты пишешь SMS-рассылку для «{name}» ({industry}).
Правила: максимум {limit} символов ВКЛЮЧАЯ пробелы, без эмодзи и без ссылок,
обязателен призыв к действию (одно из слов: {cta_examples}),
запрещены фразы: {stop_words}. Не выдумывай цены и даты.
В ответе — ТОЛЬКО текст SMS.""",
    "whatsapp": """\
Ты пишешь сообщение для WhatsApp/MAX-рассылки от «{name}» ({industry}).
Tone of voice: {tone}. 2-4 коротких предложения, 1-2 эмодзи, максимум {limit}
символов, обязателен призыв к действию (одно из слов: {cta_examples}),
запрещены фразы: {stop_words}. Не выдумывай цены и даты.
В ответе — ТОЛЬКО текст сообщения.""",
}

LIMITS = {"sms": SMS_LIMIT, "whatsapp": WA_LIMIT}


class MailingState(TypedDict):
    tenant: dict[str, Any]
    items: list[dict[str, Any]]   # {theme, channel(sms|whatsapp), slot}
    idx: int
    attempts: int
    draft: str
    feedback: str
    valid: bool
    problems: list[str]
    results: list[dict[str, Any]]


def node_next_item(state: MailingState) -> dict:
    return {"idx": state["idx"] + 1, "attempts": 0, "draft": "",
            "feedback": "", "valid": False, "problems": []}


def node_generate(state: MailingState) -> dict:
    item = state["items"][state["idx"]]
    brand = state["tenant"]["brand_profile"]
    limit = LIMITS[item["channel"]]

    llm = build_llm(temperature=0.6)
    if llm is None:
        draft = (f"{state['tenant']['name']}: {item['theme'][:60]}. Запишитесь сегодня!"
                 if item["channel"] == "sms" else
                 f"Здравствуйте! 👋 {item['theme']}. Подробности у администратора — "
                 f"запишитесь, пока есть места!")
    else:
        system = PROMPTS[item["channel"]].format(
            name=state["tenant"]["name"], industry=state["tenant"]["industry"],
            tone=brand.get("tone", "дружелюбно"),
            cta_examples=", ".join(brand.get("cta_words", ["запишитесь"])[:4]),
            stop_words="; ".join(brand.get("stop_words", [])) or "нет",
            limit=limit,
        )
        user = f"Тема рассылки: {item['theme']}"
        if state["feedback"]:
            user += (f"\n\nЗамечания к прошлой версии: {state['feedback']}\n"
                     f"Прошлая версия:\n{state['draft']}\nПерепиши с учётом замечаний.")
        draft = llm.invoke([("system", system), ("user", user)]).content.strip().strip('"')

    return {"draft": draft, "attempts": state["attempts"] + 1}


def node_validate(state: MailingState) -> dict:
    item = state["items"][state["idx"]]
    brand = state["tenant"]["brand_profile"]
    limit = LIMITS[item["channel"]]
    text, low = state["draft"], state["draft"].lower()
    problems: list[str] = []

    if len(text) > limit:
        problems.append(f"длина {len(text)} > лимита {limit}")
    if len(text) < 30:
        problems.append("подозрительно коротко (<30 символов)")
    for sw in brand.get("stop_words", []):
        if sw.lower() in low:
            problems.append(f"запрещённая фраза «{sw}»")
    cta = brand.get("cta_words", [])
    if cta and not any(c in low for c in cta):
        problems.append("нет призыва к действию")

    return {"valid": not problems, "problems": problems, "feedback": "; ".join(problems)}


def node_record(state: MailingState) -> dict:
    item = state["items"][state["idx"]]
    result = {"theme": f"{item['theme']} · отправка: {item['slot']}",
              "channel": item["channel"], "text": state["draft"],
              "status": "pending_approval" if state["valid"] else "needs_human",
              "attempts": state["attempts"], "problems": state["problems"]}
    return {"results": state["results"] + [result]}


def build_graph():
    g = StateGraph(MailingState)
    g.add_node("next_item", node_next_item)
    g.add_node("generate", node_generate)
    g.add_node("validate", node_validate)
    g.add_node("record", node_record)
    g.add_edge(START, "next_item")
    g.add_edge("next_item", "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges(
        "validate",
        lambda s: "revise" if not s["valid"] and s["attempts"] < MAX_ATTEMPTS else "record",
        {"revise": "generate", "record": "record"})
    g.add_conditional_edges(
        "record",
        lambda s: "next_item" if s["idx"] + 1 < len(s["items"]) else "finish",
        {"next_item": "next_item", "finish": END})
    return g.compile()


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    plan = db.get_plan(tenant_id)
    if not tenant or not plan:
        return "нет тенанта или пустой контент-план"

    # По каждой теме — SMS и WhatsApp-версия; слоты отправки по будням
    items = []
    for i, p in enumerate(plan):
        slot = SEND_SLOTS[i % len(SEND_SLOTS)]
        items.append({"theme": p["theme"], "channel": "sms", "slot": slot})
        items.append({"theme": p["theme"], "channel": "whatsapp", "slot": slot})

    run_id = db.start_run(tenant_id, "mailings_weekly")
    try:
        initial: MailingState = {
            "tenant": tenant, "items": items, "idx": -1, "attempts": 0,
            "draft": "", "feedback": "", "valid": False, "problems": [], "results": [],
        }
        final = build_graph().invoke(initial)
        for r in final["results"]:
            db.save_content(tenant_id, run_id, r["channel"], r["theme"], r["text"],
                            r["status"], r["problems"], r["attempts"])
        ok = sum(r["status"] == "pending_approval" for r in final["results"])
        plan_lines = "\n".join(f"- {it['slot']}: {it['theme']} (SMS + WhatsApp)"
                               for it in items[::2])
        summary = (f"план рассылок на неделю:\n{plan_lines}\n\n"
                   f"черновиков: {len(final['results'])}, прошли автопроверку: {ok} — "
                   f"ждут согласования в панели")
        db.finish_run(run_id, "done", summary)
        return f"черновиков: {len(final['results'])}, прошли автопроверку: {ok}"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
