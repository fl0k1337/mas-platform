"""Единая точка доступа к LLM. Сейчас — локальная Ollama;
в боевой версии здесь появится LiteLLM Proxy и выбор модели per-tenant."""

from __future__ import annotations

from app.config import OLLAMA_MODEL, OLLAMA_URL


def build_llm(temperature: float = 0.3):
    """Вернёт модель или None, если Ollama не запущена (скрипты не падают)."""
    try:
        import httpx
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2)
    except Exception:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=OLLAMA_MODEL, base_url=f"{OLLAMA_URL}/v1",
                      api_key="ollama", temperature=temperature)
