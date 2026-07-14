"""
Агент-Аналитик конкурентов (ежемесячный).

Единственный агент системы, который ходит во внешний интернет:
  1. Клиент задаёт в панели список конкурентов (название + URL страницы —
     сайт, страница акций, прайс).
  2. Агент скачивает каждую страницу, вычищает HTML до текста.
  3. Сравнивает с прошлым снимком из БД (таблица competitor_snapshots) —
     это «память» агента между запусками.
  4. Детерминированный diff находит новые/исчезнувшие строки, LLM
     интерпретирует: что за изменение, угроза ли это, что нам делать.
  5. Свежий снимок сохраняется — следующий запуск сравнит уже с ним.

Первый запуск по каждому конкуренту — базовый (сравнивать не с чем):
агент просто описывает, что видит, и запоминает снимок.
"""

from __future__ import annotations

import difflib
import re

from app import db, tg
from app.llm import build_llm

MAX_PAGE_CHARS = 6000     # столько текста страницы храним и передаём модели
MAX_DIFF_LINES = 25

SYSTEM_PROMPT = """\
Ты — конкурентный аналитик компании ({industry}).
Тебе передают: имя конкурента и список ИЗМЕНЕНИЙ на его странице за месяц
(строки, которые появились или исчезли), либо текст страницы, если это первый
осмотр. Твоя задача (коротко, по делу):
1. Что изменилось по сути (новые акции, цены, услуги, позиционирование).
2. Оценка угрозы: низкая / средняя / высокая — и почему.
3. Одна конкретная идея, чем нам ответить.
Опирайся ТОЛЬКО на переданный текст. Если изменения технические (меню, копирайт,
случайный мусор вёрстки) — так и скажи: «существенных изменений нет».
"""


def fetch_page_text(url: str) -> str:
    """Скачивает страницу и превращает HTML в плоский текст."""
    import httpx
    resp = httpx.get(url, timeout=20, follow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 (compatible; MASPlatform/0.6)"})
    resp.raise_for_status()
    html = resp.text
    # выкидываем скрипты/стили, затем все теги, схлопываем пробелы
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", "\n", html)
    lines = [ln.strip() for ln in html.splitlines()]
    text = "\n".join(ln for ln in lines if len(ln) > 2)
    return text[:MAX_PAGE_CHARS]


def diff_lines(old: str, new: str) -> tuple[list[str], list[str]]:
    """Строки, которые появились и исчезли (без «шумовых» коротких)."""
    old_set = {ln for ln in old.splitlines() if len(ln) > 15}
    new_set = {ln for ln in new.splitlines() if len(ln) > 15}
    added = [ln for ln in new.splitlines() if ln in (new_set - old_set)][:MAX_DIFF_LINES]
    removed = [ln for ln in old.splitlines() if ln in (old_set - new_set)][:MAX_DIFF_LINES]
    return added, removed


def analyze_competitor(comp: dict, industry: str, llm) -> str:
    """Осмотр одного конкурента. Возвращает блок отчёта."""
    try:
        current = fetch_page_text(comp["url"])
    except Exception as e:
        return f"🌐 {comp['name']} — страница недоступна ({e})"

    prev = db.last_snapshot(comp["id"])
    db.save_snapshot(comp["id"], current)

    if prev is None:
        headline = f"🆕 {comp['name']} — первый осмотр, снимок сохранён"
        if llm is None:
            return f"{headline}\n(LLM недоступна — базовое описание будет в след. запуске)"
        summary = llm.invoke([
            ("system", SYSTEM_PROMPT.format(industry=industry)),
            ("user", f"Конкурент: {comp['name']} ({comp['url']})\n"
                     f"Первый осмотр. Текст страницы:\n{current[:3500]}"),
        ]).content
        return f"{headline}\n{summary}"

    similarity = difflib.SequenceMatcher(None, prev["content_text"], current).ratio()
    added, removed = diff_lines(prev["content_text"], current)

    if not added and not removed:
        return f"✅ {comp['name']} — изменений нет (снимок от {prev['fetched_at']})"

    diff_text = ""
    if added:
        diff_text += "ПОЯВИЛОСЬ:\n" + "\n".join(f"+ {ln}" for ln in added) + "\n"
    if removed:
        diff_text += "ИСЧЕЗЛО:\n" + "\n".join(f"- {ln}" for ln in removed)

    headline = (f"🔄 {comp['name']} — страница изменилась "
                f"(совпадение с прошлым снимком {similarity:.0%})")
    if llm is None:
        return f"{headline}\n{diff_text}"
    verdict = llm.invoke([
        ("system", SYSTEM_PROMPT.format(industry=industry)),
        ("user", f"Конкурент: {comp['name']}\nИзменения за месяц:\n{diff_text}"),
    ]).content
    return f"{headline}\n{verdict}"


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    competitors = db.list_competitors(tenant_id)
    if not competitors:
        return "список конкурентов пуст — добавьте их в карточке клиента"

    run_id = db.start_run(tenant_id, "competitors_monthly")
    try:
        llm = build_llm(temperature=0.3)
        blocks = [analyze_competitor(c, tenant["industry"], llm) for c in competitors]
        report = (f"🕵️ Анализ конкурентов — {tenant['name']} "
                  f"({len(competitors)} шт.)\n\n" + "\n\n".join(blocks))
        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"осмотрено конкурентов: {len(competitors)}"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
