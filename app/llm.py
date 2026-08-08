"""
Единая точка доступа к LLM. Приоритет выбора:

  1. Ollama — если запущена локально (бесплатно, без лимитов; режим разработки).
  2. GigaChat (Сбер) — если Ollama недоступна, но в .env задан GIGACHAT_CREDENTIALS
     (облачный сервер: без GPU, из РФ, freemium-лимит).
  3. None — черновой режим: агенты работают, но вместо текстов заглушки.

Остальной код платформы не знает, какая модель под капотом, — это наша
версия LiteLLM-шлюза из спецификации в миниатюре.
"""

from __future__ import annotations

import os

from app.config import OLLAMA_MODEL, OLLAMA_URL  # заодно загружает .env

GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2")   # Lite-модель, самая дешёвая
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")  # PERS = физлицо


def _try_ollama(temperature: float):
    """Ollama, если отвечает локально. Иначе None."""
    try:
        import httpx
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2)
    except Exception:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=OLLAMA_MODEL, base_url=f"{OLLAMA_URL}/v1",
                      api_key="ollama", temperature=temperature)


def _try_gigachat(temperature: float):
    """GigaChat, если задан ключ и установлена библиотека. Иначе None."""
    if not GIGACHAT_CREDENTIALS:
        return None
    try:
        from langchain_gigachat import GigaChat
        return GigaChat(
            credentials=GIGACHAT_CREDENTIALS,
            model=GIGACHAT_MODEL,
            scope=GIGACHAT_SCOPE,
            verify_ssl_certs=False,   # сертификаты НУЦ Минцифры; для MVP отключаем
            temperature=max(temperature, 0.1),
        )
    except Exception:
        return None


def build_llm(temperature: float = 0.3):
    """Вернёт модель или None (черновой режим). Никогда не роняет агентов.
    Приоритет: Ollama (локально) → GigaChat (облако) → None."""
    return (_try_ollama(temperature)
            or _try_gigachat(temperature))
