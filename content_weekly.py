"""
Прототип №3: Агент-Копирайтер — посты на неделю под готовые медиафайлы.

Что нового по сравнению с traffic_report.py:
  1. Профиль бренда (tone of voice, стоп-слова) подмешивается в промпт —
     так один и тот же агент пишет по-разному для разных клиентов платформы.
  2. Цикл доработки: детерминированный Валидатор проверяет каждый черновик
     (длина под канал, стоп-слова, призыв к действию). Если проверка не пройдена,
     граф ВОЗВРАЩАЕТ текст Копирайтеру с конкретными замечаниями — до 2 попыток.
     Это первый условный переход (conditional edge) в вашем графе LangGraph.
  3. Результат: файл week_posts.md + черновики в Telegram с пометкой
     «на согласование» (само согласование кнопками сделаем следующим шагом).

Запуск (новых библиотек не нужно):
    python content_weekly.py
Ollama должна быть запущена. Без неё скрипт отработает на заглушках —
удобно проверять сам конвейер.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypedDict

from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).parent

# ----------------------------------------------------------------------------
# 1. ПРОФИЛЬ БРЕНДА И КОНТЕНТ-ПЛАН (в проде — из БД: tenants.brand_profile,
#    контент-план месяца от Агента-Аналитика, описания медиа от дизайнера)
# ----------------------------------------------------------------------------

BRAND = {
    "name": "Фитнес-клуб «Импульс»",
    "industry": "фитнес-клуб в спальном районе, аудитория 25-45 лет",
    "tone": "дружелюбно, энергично, на «вы», без канцелярита и пафоса, "
            "можно лёгкий юмор, 1-3 эмодзи на пост",
    "stop_words": ["гарантия результата", "лучший в городе", "самый дешёвый",
                   "№1", "уникальный"],  # юр. риски и штампы — запрещены брендбуком
    "cta_words": ["запишитесь", "приходите", "пишите", "звоните", "жмите",
                  "переходите", "забронируйте", "успейте"],
}

CONTENT_PLAN = [
    {"theme": "Открытие утренних групп по йоге с 1 августа",
     "channel": "telegram",
     "media": "фото: зал с панорамными окнами на рассвете, инструктор раскладывает коврики"},
    {"theme": "История клиентки: минус 12 кг за полгода без жёстких диет",
     "channel": "instagram",
     "media": "коллаж до/после, женщина 35 лет улыбается в тренажёрном зале"},
    {"theme": "Скидка 20% на годовой абонемент до конца июля",
     "channel": "max",
     "media": "видео 15 сек: динамичная нарезка тренировок, в конце — таймер обратного отсчёта"},
]

CHANNEL_LIMITS = {"telegram": 3500, "instagram": 2200, "max": 3500}
MAX_ATTEMPTS = 2  # попытки на пост: первая генерация + одна доработка


# ----------------------------------------------------------------------------
# 2. LLM (та же локальная Ollama, что и в traffic_report.py)
# ----------------------------------------------------------------------------

OLLAMA_MODEL = "qwen2.5:7b"


def build_llm():
    try:
        import httpx
        httpx.get("http://localhost:11434/api/tags", timeout=2)
    except Exception:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=OLLAMA_MODEL, base_url="http://localhost:11434/v1",
                      api_key="ollama", temperature=0.7)  # для креатива температура выше


COPYWRITER_SYSTEM_PROMPT = """\
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


class PostResult(BaseModel):
    theme: str
    channel: str
    text: str
    status: str          # ok | needs_human
    attempts: int
    problems: list[str] = []


# ----------------------------------------------------------------------------
# 3. СОСТОЯНИЕ ГРАФА
# ----------------------------------------------------------------------------

class ContentState(TypedDict):
    idx: int                       # какой пункт контент-плана в работе
    attempts: int                  # сколько попыток потрачено на текущий пост
    draft: str                     # текущий черновик
    feedback: str                  # замечания валидатора для доработки
    valid: bool
    problems: list[str]
    results: list[dict[str, Any]]  # готовые PostResult
    telegram_status: str


# ----------------------------------------------------------------------------
# 4. УЗЛЫ ГРАФА
# ----------------------------------------------------------------------------

def node_next_item(state: ContentState) -> dict:
    """Берём следующий пункт контент-плана, сбрасываем счётчики."""
    return {"idx": state["idx"] + 1, "attempts": 0, "draft": "",
            "feedback": "", "valid": False, "problems": []}


def node_generate(state: ContentState) -> dict:
    """Копирайтер: пишет черновик (или дорабатывает по замечаниям)."""
    item = CONTENT_PLAN[state["idx"]]
    limit = CHANNEL_LIMITS[item["channel"]]

    llm = build_llm()
    if llm is None:
        # Заглушка для отладки конвейера без Ollama
        draft = (f"[Черновик без LLM] {item['theme']}. Подробности на фото! "
                 f"Запишитесь на пробное занятие — администратор подберёт удобное время. "
                 f"Ждём вас в «Импульсе»! 💪")
    else:
        system = COPYWRITER_SYSTEM_PROMPT.format(
            name=BRAND["name"], industry=BRAND["industry"], tone=BRAND["tone"],
            cta_examples=", ".join(BRAND["cta_words"][:4]),
            stop_words="; ".join(BRAND["stop_words"]),
            limit=limit, channel=item["channel"],
        )
        user = (f"Тема поста: {item['theme']}\n"
                f"Медиафайл, под который пишем: {item['media']}")
        if state["feedback"]:
            user += (f"\n\nТвой прошлый вариант не прошёл проверку. Замечания редактора:\n"
                     f"{state['feedback']}\n"
                     f"Прошлый вариант:\n{state['draft']}\n\nПерепиши с учётом замечаний.")
        draft = llm.invoke([("system", system), ("user", user)]).content.strip()

    return {"draft": draft, "attempts": state["attempts"] + 1}


def node_validate(state: ContentState) -> dict:
    """Детерминированный Валидатор: правила брендбука, без LLM и без фантазий."""
    item = CONTENT_PLAN[state["idx"]]
    limit = CHANNEL_LIMITS[item["channel"]]
    text = state["draft"]
    text_low = text.lower()
    problems: list[str] = []

    if len(text) > limit:
        problems.append(f"слишком длинно: {len(text)} символов при лимите {limit}")
    if len(text) < 100:
        problems.append("слишком коротко, меньше 100 символов — это не пост, а подпись")
    for sw in BRAND["stop_words"]:
        if sw.lower() in text_low:
            problems.append(f"запрещённая фраза из брендбука: «{sw}»")
    if not any(cta in text_low for cta in BRAND["cta_words"]):
        problems.append("нет призыва к действию (запишитесь / приходите / звоните ...)")

    return {"valid": not problems, "problems": problems,
            "feedback": "; ".join(problems)}


def node_record(state: ContentState) -> dict:
    """Фиксируем результат по посту: прошёл проверку или требует ручной доработки."""
    item = CONTENT_PLAN[state["idx"]]
    result = PostResult(
        theme=item["theme"], channel=item["channel"], text=state["draft"],
        status="ok" if state["valid"] else "needs_human",
        attempts=state["attempts"], problems=state["problems"],
    )
    return {"results": state["results"] + [result.model_dump()]}


def node_deliver(state: ContentState) -> dict:
    """Сохраняем неделю в файл и отправляем черновики в Telegram на согласование."""
    lines = ["# Контент на неделю (черновики на согласование)\n"]
    for r in state["results"]:
        badge = "✅ прошёл проверку" if r["status"] == "ok" else \
                f"⚠ нужна ручная доработка ({'; '.join(r['problems'])})"
        lines += [f"## [{r['channel']}] {r['theme']}",
                  f"_{badge}, попыток: {r['attempts']}_\n", r["text"], "\n---\n"]
    out_file = BASE_DIR / "week_posts.md"
    out_file.write_text("\n".join(lines), encoding="utf-8")

    tg_text = "📝 Черновики постов на неделю — нужно согласование:\n\n"
    for i, r in enumerate(state["results"], 1):
        mark = "✅" if r["status"] == "ok" else "⚠"
        tg_text += f"{mark} {i}. [{r['channel']}] {r['theme']}\n\n{r['text']}\n\n{'—' * 20}\n\n"
    return {"telegram_status": send_telegram(tg_text)}


def send_telegram(text: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return "skipped: нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в .env"
    try:
        import httpx
        for i in range(0, len(text), 4000):
            httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": text[i:i + 4000]},
                       timeout=15).raise_for_status()
        return "sent"
    except Exception as e:
        return f"error: {e}"


# ----------------------------------------------------------------------------
# 5. СБОРКА ГРАФА — теперь с условными переходами
# ----------------------------------------------------------------------------

from langgraph.graph import END, START, StateGraph  # noqa: E402


def route_after_validate(state: ContentState) -> str:
    """Куда идти после проверки: на доработку или фиксировать результат."""
    if not state["valid"] and state["attempts"] < MAX_ATTEMPTS:
        return "revise"      # вернуть Копирайтеру с замечаниями
    return "record"


def route_after_record(state: ContentState) -> str:
    """Есть ли ещё посты в плане."""
    if state["idx"] + 1 < len(CONTENT_PLAN):
        return "next_item"
    return "deliver"


def build_graph():
    g = StateGraph(ContentState)
    g.add_node("next_item", node_next_item)
    g.add_node("generate", node_generate)
    g.add_node("validate", node_validate)
    g.add_node("record", node_record)
    g.add_node("deliver", node_deliver)

    g.add_edge(START, "next_item")
    g.add_edge("next_item", "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            {"revise": "generate", "record": "record"})
    g.add_conditional_edges("record", route_after_record,
                            {"next_item": "next_item", "deliver": "deliver"})
    g.add_edge("deliver", END)
    return g.compile()


# ----------------------------------------------------------------------------

if __name__ == "__main__":
    initial: ContentState = {
        "idx": -1, "attempts": 0, "draft": "", "feedback": "",
        "valid": False, "problems": [], "results": [], "telegram_status": "",
    }
    print(f"Копирайтер пишет {len(CONTENT_PLAN)} поста(ов) для «{BRAND['name']}»...")
    print("(с Ollama это займёт несколько минут — на каждый пост 1-2 обращения к модели)\n")

    final = build_graph().invoke(initial)

    for r in final["results"]:
        mark = "✅" if r["status"] == "ok" else "⚠"
        print(f"{mark} [{r['channel']}] {r['theme']} — попыток: {r['attempts']}")
        if r["problems"]:
            print(f"   проблемы: {'; '.join(r['problems'])}")
    print(f"\nФайл с текстами: {BASE_DIR / 'week_posts.md'}")
    print("Telegram:", final["telegram_status"])