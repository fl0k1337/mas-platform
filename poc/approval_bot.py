"""
Прототип №4: согласование контента кнопками в Telegram (human-in-the-loop).

Как работает связка:
  1. content_weekly.py генерирует черновики и складывает их в drafts_queue.json.
  2. Этот бот отправляет каждый черновик вам в личку с кнопками:
        ✅ Опубликовать   ✍ Переписать   ❌ Отклонить
  3. Нажали ✅ — пост НЕМЕДЛЕННО публикуется в ваш Telegram-канал.
     Нажали ✍ или ❌ — статус фиксируется в очереди (перегенерация — след. запуском
     content_weekly.py; в боевой версии граф LangGraph продолжится с чекпоинта).
  4. Когда решения приняты по всем черновикам — бот пишет итог и завершается.

Это ручная версия механизма interrupt/resume из LangGraph, о котором написано
в спецификации: агент сделал работу -> система ЖДЁТ человека -> действие
выполняется только после явного одобрения.

Настройка канала для публикации (один раз):
  1. Telegram -> Новый канал (например, «Импульс — тест»), тип: публичный,
     придумайте уникальную ссылку, например impulse_test_2foc.
  2. В канале: Управление -> Администраторы -> Добавить -> найдите вашего бота
     по username -> дайте право «Публикация сообщений».
  3. В .env добавьте строку:  TELEGRAM_CHANNEL_ID=@impulse_test_2foc
  Без этой строки бот работает в режиме репетиции: вместо публикации пишет
  «(режим репетиции — канал не настроен)».

Запуск:
    python content_weekly.py      (если очередь черновиков ещё не создана)
    python approval_bot.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).parent
QUEUE_FILE = BASE_DIR / "drafts_queue.json"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")          # ваша личка — сюда приходят кнопки
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")    # канал для публикации, напр. @impulse_test

API = f"https://api.telegram.org/bot{TOKEN}"

DECISIONS = {"approve": "✅ ОПУБЛИКОВАНО", "revise": "✍ НА ПЕРЕПИСЫВАНИЕ",
             "reject": "❌ ОТКЛОНЕНО"}


def api_call(method: str, **params) -> dict:
    """Один вызов Telegram Bot API. Вынесен в функцию, чтобы легко тестировать."""
    resp = httpx.post(f"{API}/{method}", json=params, timeout=40)
    resp.raise_for_status()
    return resp.json()["result"]


# ----------------------------------------------------------------------------
# Очередь черновиков
# ----------------------------------------------------------------------------

def load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        raise SystemExit("Файл drafts_queue.json не найден. "
                         "Сначала запустите: python content_weekly.py")
    return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))


def save_queue(queue: list[dict]) -> None:
    QUEUE_FILE.write_text(json.dumps(queue, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ----------------------------------------------------------------------------
# Шаг 1: разослать черновики с кнопками
# ----------------------------------------------------------------------------

def send_drafts_for_approval(queue: list[dict]) -> None:
    for d in queue:
        if d["approval"] != "pending":
            continue  # уже решён в прошлом запуске
        preview = d["text"] if len(d["text"]) <= 3000 else d["text"][:3000] + "…"
        warn = "" if d["status"] == "ok" else \
            f"\n\n⚠ Внимание: автопроверку не прошёл ({'; '.join(d['problems'])})"
        msg = api_call(
            "sendMessage",
            chat_id=CHAT_ID,
            text=f"📝 Черновик #{d['id']} [{d['channel']}]\n"
                 f"Тема: {d['theme']}{warn}\n\n{preview}",
            reply_markup={"inline_keyboard": [[
                {"text": "✅ Опубликовать", "callback_data": f"approve:{d['id']}"},
                {"text": "✍ Переписать", "callback_data": f"revise:{d['id']}"},
                {"text": "❌ Отклонить", "callback_data": f"reject:{d['id']}"},
            ]]},
        )
        d["approval_message_id"] = msg["message_id"]
    save_queue(queue)


# ----------------------------------------------------------------------------
# Шаг 2: публикация (вызывается по кнопке ✅)
# ----------------------------------------------------------------------------

def publish(draft: dict) -> str:
    """Публикует согласованный пост в канал. Это зачаток того самого
    Публикатора из спецификации: детерминированное действие, без LLM."""
    if not CHANNEL_ID:
        return "(режим репетиции — канал не настроен, добавьте TELEGRAM_CHANNEL_ID в .env)"
    posted = api_call("sendMessage", chat_id=CHANNEL_ID, text=draft["text"])
    return f"опубликовано в {CHANNEL_ID}, message_id={posted['message_id']}"


# ----------------------------------------------------------------------------
# Шаг 3: слушаем нажатия кнопок (long polling)
# ----------------------------------------------------------------------------

def listen_for_decisions(queue: list[dict]) -> None:
    by_id = {d["id"]: d for d in queue}
    offset = None
    print("Жду ваших решений в Telegram... (Ctrl+C — прервать, решения сохраняются)")

    while any(d["approval"] == "pending" for d in queue):
        resp = httpx.get(f"{API}/getUpdates",
                         params={"timeout": 25, **({"offset": offset} if offset else {})},
                         timeout=40)
        resp.raise_for_status()
        for upd in resp.json()["result"]:
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue  # игнорируем всё, кроме нажатий кнопок

            action, _, draft_id = cq["data"].partition(":")
            draft = by_id.get(int(draft_id))
            api_call("answerCallbackQuery", callback_query_id=cq["id"])
            if draft is None or draft["approval"] != "pending" or action not in DECISIONS:
                continue

            draft["approval"] = action
            note = ""
            if action == "approve":
                note = "\n" + publish(draft)
            save_queue(queue)  # каждое решение сразу на диск — ничего не теряем

            # Обновляем сообщение с кнопками: фиксируем решение, кнопки убираем
            api_call("editMessageText",
                     chat_id=CHAT_ID,
                     message_id=draft["approval_message_id"],
                     text=f"{DECISIONS[action]} — #{draft['id']} [{draft['channel']}] "
                          f"{draft['theme']}{note}")
            print(f"  #{draft['id']} -> {DECISIONS[action]}{note}")


# ----------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")

    queue = load_queue()
    pending_before = sum(d["approval"] == "pending" for d in queue)
    if pending_before == 0:
        raise SystemExit("В очереди нет черновиков, ждущих решения. "
                         "Запустите content_weekly.py для новой партии.")

    print(f"Черновиков на согласование: {pending_before}")
    send_drafts_for_approval(queue)
    listen_for_decisions(queue)

    print("\nИтог по очереди:")
    for d in queue:
        print(f"  #{d['id']} [{d['channel']}] {d['theme']} -> {DECISIONS.get(d['approval'], d['approval'])}")
    print("\nГотово. ✍-черновики перегенерируются при следующем запуске content_weekly.py.")