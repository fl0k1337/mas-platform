"""
Единая точка доступа к LLM. Приоритет выбора:

  1. GigaChat (Сбер) — если в .env задан GIGACHAT_CREDENTIALS.
     Так работает облачный сервер: без GPU, из РФ, с freemium-лимитом.
  2. Ollama — если запущена локально (режим разработки на вашем ПК).
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


def build_llm(temperature: float = 0.3):
    """Вернёт модель или None (черновой режим). Никогда не роняет агентов."""
    if GIGACHAT_CREDENTIALS:
        try:
            from langchain_gigachat import GigaChat
            return GigaChat(
                credentials=GIGACHAT_CREDENTIALS,
                model=GIGACHAT_MODEL,
                scope=GIGACHAT_SCOPE,
                # у GigaChat сертификаты НУЦ Минцифры, которых нет в системе —
                # для MVP отключаем проверку; в проде поставим их корневой серт
                verify_ssl_certs=False,
                temperature=max(temperature, 0.1),
            )
        except Exception:
            pass  # нет библиотеки или битый ключ — попробуем Ollama ниже

    try:
        import httpx
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2)
    except Exception:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=OLLAMA_MODEL, base_url=f"{OLLAMA_URL}/v1",
                      api_key="ollama", temperature=temperature)