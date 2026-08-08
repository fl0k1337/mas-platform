"""
Агент контент-плана (ежемесячный).

Замыкает цепочку из процесса владельца: анализ конкурентов → контент-план →
посты. Раньше темы вбивались в план руками; теперь агент предлагает их сам,
опираясь на профиль бренда и НА ПОСЛЕДНИЙ ОТЧЁТ ПО КОНКУРЕНТАМ (если он был).

Что делает:
  1. Берёт профиль клиента, текущий план и последний отчёт агента конкурентов.
  2. Нейросеть предлагает темы на месяц: тема, канал, идея медиафайла, зачем это.
  3. ДЕТЕРМИНИРОВАННО: отбрасывает дубли к уже существующему плану, приводит
     канал к допустимому, режет слишком длинные темы.
  4. Добавляет позиции в контент-план клиента — их сразу видно в карточке,
     лишнее можно удалить кнопкой. После этого «Контент недели» напишет посты.

Ничего не публикуется: план — это входные данные для копирайтера, а не контент.
"""

from __future__ import annotations

from app import db, tg
from app.llm import build_llm

TOPICS_COUNT = 8                 # сколько тем просим на месяц
CHANNELS = ("telegram", "instagram", "max")
THEME_MAX = 120

SYSTEM_PROMPT = """\
Ты — контент-стратег бренда «{name}» ({industry}).
Tone of voice: {tone}.

Составь контент-план на месяц: {n} тем для соцсетей.
Каналы только из списка: {channels}.

{competitor_block}
{existing_block}

Ответь СТРОГО блоками, ничего лишнего:

ТЕМА: <о чём пост, до 100 символов>
КАНАЛ: <один из: {channels}>
МЕДИА: <что снять или нарисовать дизайнеру, одно предложение>
ЗАЧЕМ: <какую задачу бизнеса решает этот пост, коротко>
---

Правила: темы РАЗНЫЕ по смыслу и не повторяют уже существующие.
Не выдумывай акции, цены и даты, которых нет в задании.
ЗАПРЕЩЕНЫ фразы: {stop_words}. Пиши по-русски.
"""


def _parse_blocks(answer: str) -> list[dict]:
    keys = {"ТЕМА": "theme", "КАНАЛ": "channel", "МЕДИА": "media", "ЗАЧЕМ": "why"}
    blocks, cur = [], {}
    for raw in answer.splitlines():
        stripped = raw.strip()
        # разделитель блоков проверяем ДО срезания маркеров списка,
        # иначе «---» превратится в пустую строку и блоки склеятся
        if stripped.startswith("---"):
            if cur.get("theme"):
                blocks.append(cur)
            cur = {}
            continue
        line = stripped.lstrip("*#-— ").strip()
        for ru, en in keys.items():
            if line.upper().startswith(ru + ":"):
                cur[en] = line.split(":", 1)[1].strip()
                break
    if cur.get("theme"):
        blocks.append(cur)
    return blocks


def _normalize_channel(raw: str | None) -> str:
    """Приводим канал к допустимому: модель любит писать «Телеграм», «TG» и т.п."""
    s = (raw or "").lower()
    if "insta" in s or "инст" in s:
        return "instagram"
    if "max" in s or "макс" in s:
        return "max"
    return "telegram"


def _dedupe(blocks: list[dict], existing: list[str]) -> tuple[list[dict], int]:
    """Убираем повторы: и к существующему плану, и внутри самой выдачи."""
    def norm(s: str) -> set[str]:
        return {w for w in s.lower().replace(",", " ").split() if len(w) > 3}

    seen = [norm(e) for e in existing]
    fresh, dropped = [], 0
    for b in blocks:
        words = norm(b["theme"])
        if not words:
            dropped += 1
            continue
        # похоже, если больше половины значимых слов совпадает с уже имеющейся темой
        if any(len(words & s) >= max(2, len(words) // 2) for s in seen):
            dropped += 1
            continue
        seen.append(words)
        fresh.append(b)
    return fresh, dropped


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    run_id = db.start_run(tenant_id, "content_plan")
    try:
        brand = tenant["brand_profile"]
        existing = [p["theme"] for p in db.get_plan(tenant_id)]

        competitor_report = db.last_run_output(tenant_id, "competitors_monthly")
        if competitor_report:
            competitor_block = ("Свежие наблюдения по конкурентам — используй их, чтобы "
                                "предложить темы, которыми мы будем отличаться:\n"
                                f"{competitor_report[:2500]}")
        else:
            competitor_block = ("Данных по конкурентам нет — опирайся на отрасль "
                                "и потребности аудитории.")
        existing_block = ("Уже есть в плане (НЕ повторять):\n- " + "\n- ".join(existing[:20])
                          if existing else "План пока пуст.")

        llm = build_llm(temperature=0.7)
        if llm is None:
            blocks = [{"theme": "[без нейросети] пример темы месяца",
                       "channel": "telegram", "media": "—", "why": "—"}]
        else:
            answer = llm.invoke([
                ("system", SYSTEM_PROMPT.format(
                    name=tenant["name"], industry=tenant["industry"] or "услуги",
                    tone=brand.get("tone", "нейтрально"), n=TOPICS_COUNT,
                    channels=", ".join(CHANNELS),
                    competitor_block=competitor_block, existing_block=existing_block,
                    stop_words="; ".join(brand.get("stop_words", [])) or "нет")),
                ("user", "Составь контент-план на месяц."),
            ]).content
            blocks = _parse_blocks(answer)

        for b in blocks:
            b["theme"] = b["theme"][:THEME_MAX]
            b["channel"] = _normalize_channel(b.get("channel"))

        fresh, dropped = _dedupe(blocks, existing)
        for b in fresh:
            db.add_plan_item(tenant_id, b["theme"], b["channel"], b.get("media", ""))

        lines = [f"🗓 Контент-план — {tenant['name']}",
                 f"Добавлено тем: {len(fresh)}"
                 + (f", отброшено повторов: {dropped}" if dropped else ""),
                 "Источник: " + ("анализ конкурентов + профиль бренда"
                                 if competitor_report else "профиль бренда"), ""]
        for b in fresh:
            lines.append(f"▸ [{b['channel']}] {b['theme']}")
            if b.get("media"):
                lines.append(f"   медиа: {b['media']}")
            if b.get("why"):
                lines.append(f"   зачем: {b['why']}")
        if not fresh:
            lines.append("Новых тем не добавлено — всё предложенное уже есть в плане.")
        else:
            lines.append("\nПлан в карточке клиента: лишнее можно удалить, "
                         "потом запустить «Контент недели».")

        report = "\n".join(lines)
        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"план: добавлено {len(fresh)} тем" + (f", повторов {dropped}" if dropped else "")
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
