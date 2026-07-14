"""
Прототип №5: Агент-Продюсер — ТЗ дизайнеру в Google Таблицу.

Что делает:
  1. Берёт контент-план недели (тот же, что у Копирайтера, — импортируется
     из content_weekly.py, чтобы не дублировать данные).
  2. Для каждой позиции LLM-продюсер составляет: описание макета для дизайнера,
     короткий текст на самом креативе и идею референса.
  3. Детерминированная часть добавляет технику: формат и размеры под канал,
     дедлайн, статус — и выкладывает всё листом в Google Таблицу.
  4. Ссылка на готовое ТЗ уходит в Telegram.

Если Google ещё не настроен (нет google_key.json / GOOGLE_SHEET_ID) — скрипт
не падает, а сохраняет ТЗ в файл design_briefs.csv рядом с собой. Удобно
проверить конвейер до возни с сервисным аккаунтом.

Запуск:
    pip install gspread
    python design_brief.py
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).parent
KEY_FILE = BASE_DIR / "google_key.json"   # ключ сервисного аккаунта (НЕ коммитить!)
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")   # id таблицы из её адреса

# Контент-план и бренд берём у Копирайтера — единый источник правды
from content_weekly import BRAND, CONTENT_PLAN, build_llm  # noqa: E402

# ----------------------------------------------------------------------------
# 1. ТЕХНИЧЕСКИЕ СПЕЦИФИКАЦИИ КАНАЛОВ (детерминированная часть ТЗ)
# ----------------------------------------------------------------------------

CHANNEL_SPECS = {
    "telegram":  {"format": "статичный пост", "size": "1280x720 px, JPG/PNG"},
    "instagram": {"format": "пост в ленту",   "size": "1080x1350 px, JPG"},
    "max":       {"format": "видео-кавер/пост", "size": "1080x1080 px, до 15 сек если видео"},
}
DEADLINE_DAYS = 3

HEADER = ["№", "Дата ТЗ", "Канал", "Формат", "Размер", "Тема",
          "Задача для дизайнера", "Текст на макете", "Референс",
          "Исходник от клиента", "Дедлайн", "Статус"]

PRODUCER_SYSTEM_PROMPT = """\
Ты — арт-продюсер бренда «{name}» ({industry}).
Составляешь ТЗ дизайнеру по позиции контент-плана.

Ответь СТРОГО в три строки, без чего-либо ещё:
ОПИСАНИЕ: <2-3 предложения: что изобразить на макете, настроение, композиция>
ТЕКСТ: <короткая надпись на самом креативе, максимум 8 слов>
РЕФЕРЕНС: <одно предложение: на что похоже по стилю>

Не выдумывай цены и даты, которых нет в теме. Пиши по-русски.
"""


def make_brief(item: dict, llm) -> dict:
    """LLM-часть ТЗ по одной позиции плана (+ разбор ответа с подстраховкой)."""
    if llm is None:
        return {"description": f"[без LLM] Макет по теме: {item['theme']}. "
                               f"Основа — медиа: {item['media']}",
                "overlay_text": item["theme"][:60], "reference": "—"}

    system = PRODUCER_SYSTEM_PROMPT.format(name=BRAND["name"], industry=BRAND["industry"])
    user = f"Тема: {item['theme']}\nИмеющееся медиа от клиента: {item['media']}"
    answer = llm.invoke([("system", system), ("user", user)]).content.strip()

    # Разбор трёх строк; если модель нарушила формат — кладём весь ответ в описание
    parsed = {"description": answer, "overlay_text": "", "reference": ""}
    for line in answer.splitlines():
        low = line.lower()
        if low.startswith("описание:"):
            parsed["description"] = line.split(":", 1)[1].strip()
        elif low.startswith("текст:"):
            parsed["overlay_text"] = line.split(":", 1)[1].strip()
        elif low.startswith("референс:"):
            parsed["reference"] = line.split(":", 1)[1].strip()
    return parsed


def build_rows() -> list[list[str]]:
    llm = build_llm()
    today = datetime.now()
    deadline = (today + timedelta(days=DEADLINE_DAYS)).strftime("%d.%m.%Y")
    rows = []
    for i, item in enumerate(CONTENT_PLAN, 1):
        spec = CHANNEL_SPECS.get(item["channel"], {"format": "уточнить", "size": "уточнить"})
        print(f"  составляю ТЗ {i}/{len(CONTENT_PLAN)}: {item['theme'][:50]}...")
        brief = make_brief(item, llm)
        rows.append([
            str(i), today.strftime("%d.%m.%Y"), item["channel"],
            spec["format"], spec["size"], item["theme"],
            brief["description"], brief["overlay_text"], brief["reference"],
            item["media"], deadline, "новое",
        ])
    return rows


# ----------------------------------------------------------------------------
# 2. ВЫГРУЗКА: Google Sheets, а при отсутствии настроек — CSV
# ----------------------------------------------------------------------------

def push_to_google(rows: list[list[str]]) -> str:
    import gspread
    gc = gspread.service_account(filename=str(KEY_FILE))
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.add_worksheet(title=f"ТЗ {datetime.now():%d.%m %H-%M}", rows=100, cols=15)
    ws.append_rows([HEADER] + rows)
    ws.format("A1:L1", {"textFormat": {"bold": True}})
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"


def save_csv_fallback(rows: list[list[str]]) -> str:
    out = BASE_DIR / "design_briefs.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig — чтобы Excel понял русский
        writer = csv.writer(f, delimiter=";")
        writer.writerow(HEADER)
        writer.writerows(rows)
    return str(out)


def send_telegram(text: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return "skipped"
    try:
        import httpx
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": text}, timeout=15).raise_for_status()
        return "sent"
    except Exception as e:
        return f"error: {e}"


# ----------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Продюсер составляет ТЗ по {len(CONTENT_PLAN)} позициям плана...")
    rows = build_rows()

    if KEY_FILE.exists() and SHEET_ID:
        link = push_to_google(rows)
        where = f"Google Таблица: {link}"
    else:
        path = save_csv_fallback(rows)
        where = (f"Google не настроен — сохранил в {path}\n"
                 f"(настройте google_key.json и GOOGLE_SHEET_ID в .env, см. инструкцию)")

    print(f"\nГотово. {where}")
    print("Telegram:", send_telegram(f"📐 ТЗ дизайнеру на неделю готово:\n{where}"))