"""
Агент УТП: таблица уникальных торговых предложений для рекламы.

Что делает (ежемесячная задача из списка владельца — раньше велась руками):
  1. По профилю клиента (отрасль, аудитория, tone of voice) нейросеть предлагает
     сегменты аудитории и под каждый — боль, УТП, заголовок и текст объявления,
     типичное возражение и ответ на него.
  2. ДЕТЕРМИНИРОВАННЫЙ валидатор проверяет то, что нельзя доверять нейросети:
     лимиты символов рекламных площадок (Яндекс Директ: заголовок 56, текст 81)
     и запрещённые фразы из брендбука. Нарушения не выбрасываются, а помечаются
     в колонке «Проверка» — человек видит, что править.
  3. Результат пишется НОВЫМ ЛИСТОМ в Google Таблицу клиента (лист с датой,
     старые не затираются) + краткая сводка в Telegram.

Если таблица не подключена — таблица УТП всё равно составляется и целиком
уходит в Telegram, чтобы работу можно было использовать сразу.
"""

from __future__ import annotations

from datetime import datetime

from app import db, tg
from app.integrations.service import get_utp_sheet_id
from app.llm import build_llm

SEGMENTS_COUNT = 5           # сколько сегментов аудитории просим
TITLE_LIMIT = 56             # Яндекс Директ: заголовок 1
TEXT_LIMIT = 81              # Яндекс Директ: текст объявления

HEADER = ["Сегмент аудитории", "Боль / потребность", "УТП",
          "Заголовок объявления", "Текст объявления",
          "Возражение", "Ответ на возражение", "Проверка"]

SYSTEM_PROMPT = """\
Ты — маркетолог-стратег компании «{name}» ({industry}).
Tone of voice: {tone}.

Составь {n} РАЗНЫХ сегментов аудитории и под каждый — рекламное предложение.

Ответь СТРОГО блоками, каждый блок в таком виде и ничего лишнего:

СЕГМЕНТ: <кто это, 3-6 слов>
БОЛЬ: <главная боль или потребность этого сегмента, одно предложение>
УТП: <уникальное предложение именно для него, одно предложение>
ЗАГОЛОВОК: <заголовок объявления, СТРОГО не длиннее {title_limit} символов>
ТЕКСТ: <текст объявления, СТРОГО не длиннее {text_limit} символов>
ВОЗРАЖЕНИЕ: <что мешает купить, коротко>
ОТВЕТ: <как снимаем это возражение, коротко>
---

Правила: не выдумывай цены, сроки и гарантии, которых нет в задании.
ЗАПРЕЩЕНЫ фразы: {stop_words}. Пиши по-русски. Между блоками — строка «---».
"""


def _parse_blocks(answer: str) -> list[dict]:
    """Разбор ответа модели в список словарей. Терпим к вольностям формата."""
    keys = {"СЕГМЕНТ": "segment", "БОЛЬ": "pain", "УТП": "utp",
            "ЗАГОЛОВОК": "title", "ТЕКСТ": "text",
            "ВОЗРАЖЕНИЕ": "objection", "ОТВЕТ": "answer"}
    blocks, cur = [], {}
    for raw in answer.splitlines():
        line = raw.strip().lstrip("*# ").strip()
        if line.startswith("---"):
            if cur.get("segment"):
                blocks.append(cur)
            cur = {}
            continue
        for ru, en in keys.items():
            if line.upper().startswith(ru + ":"):
                cur[en] = line.split(":", 1)[1].strip()
                break
    if cur.get("segment"):
        blocks.append(cur)
    return blocks


def _check(row: dict, stop_words: list[str]) -> str:
    """Детерминированная проверка: лимиты площадок и стоп-слова брендбука."""
    problems = []
    title, text = row.get("title", ""), row.get("text", "")
    if len(title) > TITLE_LIMIT:
        problems.append(f"заголовок {len(title)}/{TITLE_LIMIT} — сократить")
    if len(text) > TEXT_LIMIT:
        problems.append(f"текст {len(text)}/{TEXT_LIMIT} — сократить")
    if not title:
        problems.append("нет заголовка")
    joined = " ".join(str(v) for v in row.values()).lower()
    for sw in stop_words:
        if sw.lower() in joined:
            problems.append(f"запрещённая фраза «{sw}»")
    return "; ".join(problems) if problems else "ок"


def build_rows(tenant: dict) -> tuple[list[list[str]], int]:
    """Сгенерировать строки таблицы УТП. Возвращает (строки, сколько с проблемами)."""
    brand = tenant["brand_profile"]
    stop_words = brand.get("stop_words", [])

    llm = build_llm(temperature=0.6)
    if llm is None:
        blocks = [{"segment": "[без нейросети] пример сегмента",
                   "pain": "—", "utp": "—", "title": "—", "text": "—",
                   "objection": "—", "answer": "—"}]
    else:
        answer = llm.invoke([
            ("system", SYSTEM_PROMPT.format(
                name=tenant["name"], industry=tenant["industry"] or "услуги",
                tone=brand.get("tone", "нейтрально"), n=SEGMENTS_COUNT,
                title_limit=TITLE_LIMIT, text_limit=TEXT_LIMIT,
                stop_words="; ".join(stop_words) or "нет")),
            ("user", "Составь таблицу УТП."),
        ]).content
        blocks = _parse_blocks(answer)

    rows, bad = [], 0
    for b in blocks:
        verdict = _check(b, stop_words)
        if verdict != "ок":
            bad += 1
        rows.append([b.get("segment", ""), b.get("pain", ""), b.get("utp", ""),
                     b.get("title", ""), b.get("text", ""),
                     b.get("objection", ""), b.get("answer", ""), verdict])
    return rows, bad


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    run_id = db.start_run(tenant_id, "utp_table")
    try:
        rows, bad = build_rows(tenant)
        if not rows:
            db.finish_run(run_id, "failed", "нейросеть не вернула ни одного сегмента")
            return "не удалось составить УТП — попробуйте ещё раз"

        title = f"УТП {datetime.now():%d.%m.%Y}"
        sheet_id = get_utp_sheet_id(tenant_id)
        link = None
        if sheet_id:
            try:
                from app.integrations.sheets import write_table
                link = write_table(sheet_id, title, HEADER, rows)
            except Exception as e:
                tg.notify(f"⚠ УТП составлены, но не записались в Google Таблицу: {e}")

        summary = [f"🎯 Таблица УТП — {tenant['name']}",
                   f"Сегментов: {len(rows)}"
                   + (f", требуют правки: {bad}" if bad else ", все прошли проверку")]
        if link:
            summary.append(f"Таблица: {link}")
        else:
            summary.append("(Google Таблица не подключена — содержимое ниже)")
            for r in rows:
                summary.append(f"\n▸ {r[0]}\n  Боль: {r[1]}\n  УТП: {r[2]}\n"
                               f"  Заголовок: {r[3]}\n  Текст: {r[4]}\n"
                               f"  Возражение: {r[5]} → {r[6]}\n  Проверка: {r[7]}")
        report = "\n".join(summary)

        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"УТП готовы: сегментов {len(rows)}" + (f", с замечаниями {bad}" if bad else "")
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
