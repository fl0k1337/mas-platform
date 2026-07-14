"""Настройки приложения. Всё берётся из .env в корне проекта."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent   # корень проекта
DATA_DIR = BASE_DIR / "data"                        # база данных и файлы (в .gitignore)
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "mas.db"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")


def _load_secret_key() -> str:
    """Ключ подписи сессий. Берём из .env (SECRET_KEY=...), а если его нет —
    генерируем один раз и храним в data/.secret_key (файл вне Git)."""
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    import secrets
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key


SECRET_KEY = _load_secret_key()
