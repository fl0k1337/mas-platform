"""
Агент ТЗ дизайнеру (еженедельный).

Раньше владелец писал ТЗ руками и передавал через Google-таблицу — теперь это
делает система. По каждой позиции контент-плана агент составляет задание:
что изобразить, какой текст вынести на макет, на что ориентироваться по стилю.

Разделение как везде в платформе:
  • нейросеть — творческая часть (описание макета, надпись, референс);
  • КОД — техническая: формат и размеры под канал, дедлайн, статус, проверка
    длины надписи на макете (длинные надписи на креативе не читаются).

Результат — новый лист в Google Таблице клиента (не затирает прошлые ТЗ)
плюс сводка в Telegram. Если таблица не подключена, ТЗ целиком уходит в Telegram.

Про Figma честно: REST API Figma не умеет создавать макеты программно —
собрать дизайн «сам по себе» невозможно. Реалистичный путь — плагин Figma,
который по кнопке подтягивает эти тексты в шаблон. Здесь мы готовим ТЗ.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import db, tg
from app.integrations.service import get_briefs_sheet_id
from app.llm import build_llm

DEADLINE_DAYS = 3
OVERLAY_LIMIT = 60           # надпись на макете длиннее — плохо читается

CHANNEL_SPECS = {
    "telegram":  ("статичный пост",   "1280×720 px, JPG/PNG"),
    "instagram": ("пост в ленту",     "1080×1350 px, JPG"),
    "max":       ("пост / видеокавер", "1080×1080 px, видео до 15 сек"),
}

HEADER = ["№", "Дата ТЗ", "Канал", "Формат", "Размеры", "Тема",
          "Задача для дизайнера", "Текст на макете", "Референс",
          "Исходники от клиента", "Дедлайн", "Статус", "Проверка"]

SYSTEM_PROMPT = """\
Ты — арт-директор бренда «{name}» ({industry}).
Tone of voice бренда: {tone}.

Составь ТЗ дизайнеру по одной позиции контент-плана.
Ответь СТРОГО тремя строками, без пояснений:

ОПИСАНИЕ: <2-3 предложения: что изобразить, настроение, композиция>
НАДПИСЬ: <короткий текст на самом макете, не длиннее {overlay_limit} символов>
РЕФЕРЕНС: <одно предложение: на что похоже по стилю>

Не выдумывай цены, скидки и даты, которых нет в теме.
ЗАПРЕЩЕНЫ фразы: {stop_words}. Пиши по-русски.
"""


def _parse(answer: str) -> dict:
    """Разбор ответа модели; если формат нарушен — весь текст идёт в описание."""
    keys = {"ОПИСАНИЕ": "description", "НАДПИСЬ": "overlay", "РЕФЕРЕНС": "reference"}
    out = {"description": answer.strip(), "overlay": "", "reference": ""}
    found = False
    for raw in answer.splitlines():
        line = raw.strip().lstrip("*#-— ").strip()
        for ru, en in keys.items():
            if line.upper().startswith(ru + ":"):
                out[en] = line.split(":", 1)[1].strip()
                found = True
                break
    if not found:
        out["description"] = answer.strip()[:600]
    return out


def _check(brief: dict, stop_words: list[str]) -> str:
    problems = []
    if not brief.get("description"):
        problems.append("нет описания макета")
    overlay = brief.get("overlay", "")
    if len(overlay) > OVERLAY_LIMIT:
        problems.append(f"надпись {len(overlay)}/{OVERLAY_LIMIT} — сократить")
    joined = " ".join(str(v) for v in brief.values()).lower()
    for sw in stop_words:
        if sw.lower() in joined:
            problems.append(f"запрещённая фраза «{sw}»")
    return "; ".join(problems) if problems else "ок"


def build_rows(tenant: dict, plan: list[dict]) -> tuple[list[list[str]], int]:
    brand = tenant["brand_profile"]
    stop_words = brand.get("stop_words", [])
    llm = build_llm(temperature=0.6)
    today = datetime.now()
    deadline = (today + timedelta(days=DEADLINE_DAYS)).strftime("%d.%m.%Y")

    rows, bad = [], 0
    for i, item in enumerate(plan, 1):
        fmt, size = CHANNEL_SPECS.get(item["channel"], ("уточнить", "уточнить"))
        if llm is None:
            brief = {"description": f"[без нейросети] Макет по теме: {item['theme']}. "
                                    f"Исходник: {item['media'] or 'нет'}",
                     "overlay": item["theme"][:OVERLAY_LIMIT], "reference": "—"}
        else:
            answer = llm.invoke([
                ("system", SYSTEM_PROMPT.format(
                    name=tenant["name"], industry=tenant["industry"] or "услуги",
                    tone=brand.get("tone", "нейтрально"), overlay_limit=OVERLAY_LIMIT,
                    stop_words="; ".join(stop_words) or "нет")),
                ("user", f"Тема: {item['theme']}\n"
                         f"Материалы от клиента: {item['media'] or 'нет'}\n"
                         f"Канал: {item['channel']} ({fmt}, {size})"),
            ]).content
            brief = _parse(answer)

        verdict = _check(brief, stop_words)
        if verdict != "ок":
            bad += 1
        rows.append([str(i), today.strftime("%d.%m.%Y"), item["channel"], fmt, size,
                     item["theme"], brief["description"], brief["overlay"],
                     brief["reference"], item["media"] or "—", deadline, "новое", verdict])
    return rows, bad


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    plan = db.get_plan(tenant_id)
    if not plan:
        return "контент-план пуст — сначала запустите «Контент-план на месяц»"

    run_id = db.start_run(tenant_id, "design_brief")
    try:
        rows, bad = build_rows(tenant, plan)
        title = f"ТЗ {datetime.now():%d.%m.%Y}"
        sheet_id = get_briefs_sheet_id(tenant_id)
        link = None
        if sheet_id:
            try:
                from app.integrations.sheets import write_table
                link = write_table(sheet_id, title, HEADER, rows)
            except Exception as e:
                tg.notify(f"⚠ ТЗ составлены, но не записались в Google Таблицу: {e}")

        summary = [f"📐 ТЗ дизайнеру — {tenant['name']}",
                   f"Заданий: {len(rows)}, дедлайн: {rows[0][10] if rows else '—'}"
                   + (f", требуют правки: {bad}" if bad else "")]
        if link:
            summary.append(f"Таблица: {link}")
        else:
            summary.append("(Google Таблица не подключена — задания ниже)")
            for r in rows:
                summary.append(f"\n▸ [{r[2]}] {r[5]}\n  Формат: {r[3]}, {r[4]}\n"
                               f"  Задача: {r[6]}\n  Надпись на макете: {r[7]}\n"
                               f"  Референс: {r[8]}\n  Проверка: {r[12]}")
        report = "\n".join(summary)

        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"ТЗ готовы: заданий {len(rows)}" + (f", с замечаниями {bad}" if bad else "")
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
