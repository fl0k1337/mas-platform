"""
Настройки платформы, редактируемые через панель.

Зачем: раньше токены жили только в файле `.env` на сервере — чтобы поменять
токен бота, нужно было лезть в консоль и перезапускать службы. Теперь значения
хранятся в БД и читаются В МОМЕНТ ИСПОЛЬЗОВАНИЯ, поэтому изменения применяются
сразу, без перезапуска.

Порядок поиска значения:
  1. таблица `settings` в БД (то, что ввели в панели);
  2. переменная окружения / `.env` (старый способ — остаётся как запасной);
  3. значение по умолчанию.

Секреты хранятся в БД в открытом виде — так же, как раньше в `.env`. Уровень
защиты тот же: файл базы лежит на сервере рядом. Отдельное шифрование появится,
когда у платформы будет несколько операторов с разными правами.
"""

from __future__ import annotations

import os

from app import db

SECRET, TEXT, FLAG = "secret", "text", "flag"

# Описание всех настроек: что показываем в панели и как проверяем.
DEFS: list[dict] = [
    {"key": "TELEGRAM_BOT_TOKEN", "group": "Telegram", "kind": SECRET,
     "title": "Токен бота",
     "hint": "Создайте бота у @BotFather и вставьте выданный токен"},
    {"key": "TELEGRAM_CHAT_ID", "group": "Telegram", "kind": TEXT,
     "title": "Ваш chat_id (куда слать отчёты)",
     "hint": "Напишите боту любое сообщение и нажмите «Определить» справа"},
    {"key": "TELEGRAM_CHANNEL_ID", "group": "Telegram", "kind": TEXT,
     "title": "Канал для публикаций",
     "hint": "Вида @mychannel. Бот должен быть админом канала с правом публикации"},

    {"key": "GIGACHAT_CREDENTIALS", "group": "Нейросеть", "kind": SECRET,
     "title": "Ключ GigaChat (облако)",
     "hint": "developers.sber.ru → проект GigaChat API → Ключ авторизации. "
             "Используется, если локальная Ollama недоступна"},
    {"key": "GIGACHAT_MODEL", "group": "Нейросеть", "kind": TEXT,
     "title": "Модель GigaChat", "default": "GigaChat-2",
     "hint": "GigaChat-2 — самая дешёвая, её хватает для текстов"},
    {"key": "OLLAMA_URL", "group": "Нейросеть", "kind": TEXT,
     "title": "Адрес Ollama (локально)", "default": "http://localhost:11434",
     "hint": "Только для разработки на своём компьютере"},
    {"key": "OLLAMA_MODEL", "group": "Нейросеть", "kind": TEXT,
     "title": "Модель Ollama", "default": "qwen2.5:7b", "hint": ""},

    {"key": "DELIVERY_DRY_RUN", "group": "Поведение", "kind": FLAG,
     "title": "Режим репетиции",
     "hint": "Ничего никуда не отправляется — система только показывает, "
             "что отправила бы. Удобно при настройке"},
    {"key": "INSTAGRAM_ENABLED", "group": "Поведение", "kind": FLAG,
     "title": "Разрешить публикацию в Instagram",
     "hint": "По умолчанию выключено: Meta признана в РФ экстремистской "
             "организацией. Решение принимает владелец бизнеса"},
]

BY_KEY = {d["key"]: d for d in DEFS}
_cache: dict[str, str] = {}
_loaded = False


def _load() -> None:
    global _loaded
    with db.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings ("
                     "key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', "
                     "updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))")
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    _cache.clear()
    _cache.update({r["key"]: r["value"] for r in rows})
    _loaded = True


def get(key: str, default: str = "") -> str:
    """Значение настройки: БД → окружение/.env → значение по умолчанию."""
    if not _loaded:
        _load()
    val = _cache.get(key)
    if val:
        return val
    env = os.getenv(key)
    if env:
        return env.strip()
    return BY_KEY.get(key, {}).get("default", default)


def flag(key: str) -> bool:
    return get(key).strip().lower() in ("1", "true", "yes", "on")


def set_many(values: dict[str, str]) -> int:
    """Сохранить настройки. Пустая строка = удалить (вернуться к .env)."""
    if not _loaded:
        _load()
    changed = 0
    with db.connect() as conn:
        for key, raw in values.items():
            if key not in BY_KEY:
                continue
            val = (raw or "").strip()
            if val:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=datetime('now','localtime')", (key, val))
            else:
                conn.execute("DELETE FROM settings WHERE key=?", (key,))
            changed += 1
    _load()                     # сбрасываем кэш — изменения применяются сразу
    return changed


def masked(key: str) -> str:
    """Секрет для показа в панели: видно только хвост."""
    val = get(key)
    if not val:
        return ""
    return "•" * 8 + val[-4:] if len(val) > 6 else "•" * len(val)


def source(key: str) -> str:
    """Откуда сейчас берётся значение — чтобы оператор не путался."""
    if not _loaded:
        _load()
    if _cache.get(key):
        return "панель"
    if os.getenv(key):
        return ".env на сервере"
    return "не задано"


def for_panel() -> list[dict]:
    """Данные для страницы настроек, сгруппированные по разделам."""
    groups: dict[str, list[dict]] = {}
    for d in DEFS:
        item = dict(d)
        item["source"] = source(d["key"])
        item["is_set"] = bool(get(d["key"]))
        item["value"] = "" if d["kind"] == SECRET else get(d["key"])
        item["masked"] = masked(d["key"]) if d["kind"] == SECRET else ""
        item["enabled"] = flag(d["key"]) if d["kind"] == FLAG else False
        groups.setdefault(d["group"], []).append(item)
    # ключ намеренно НЕ "items": в Jinja `g.items` подхватил бы метод словаря
    return [{"name": g, "rows": rows} for g, rows in groups.items()]
