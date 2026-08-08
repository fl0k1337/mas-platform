"""
Единая точка доступа к LLM. Приоритет выбора:

  1. Ollama — если запущена локально (бесплатно, без лимитов; разработка).
  2. GigaChat (Сбер) — если Ollama недоступна, но задан ключ (так работает сервер).
  3. None — черновой режим: агенты работают, но вместо текстов заглушки.

Настройки читаются через app.settings (панель → БД → .env), поэтому смена
ключа в панели действует сразу, без перезапуска служб.
"""

from __future__ import annotations

from app import settings


def _try_ollama(temperature: float):
    url = settings.get("OLLAMA_URL", "http://localhost:11434")
    try:
        import httpx
        httpx.get(f"{url}/api/tags", timeout=2)
    except Exception:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=settings.get("OLLAMA_MODEL", "qwen2.5:7b"),
                      base_url=f"{url}/v1", api_key="ollama", temperature=temperature)


def _try_gigachat(temperature: float):
    creds = settings.get("GIGACHAT_CREDENTIALS")
    if not creds:
        return None
    try:
        from langchain_gigachat import GigaChat
        return GigaChat(
            credentials=creds,
            model=settings.get("GIGACHAT_MODEL", "GigaChat-2"),
            scope=settings.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            verify_ssl_certs=False,   # сертификаты НУЦ Минцифры; для MVP отключаем
            temperature=max(temperature, 0.1),
        )
    except Exception:
        return None


def build_llm(temperature: float = 0.3):
    """Вернёт модель или None (черновой режим). Никогда не роняет агентов."""
    return _try_ollama(temperature) or _try_gigachat(temperature)


def current_provider() -> str:
    """Что сейчас используется — для страницы настроек."""
    if _try_ollama(0.3) is not None:
        return f"Ollama ({settings.get('OLLAMA_MODEL', 'qwen2.5:7b')}), локально"
    if settings.get("GIGACHAT_CREDENTIALS"):
        return f"GigaChat ({settings.get('GIGACHAT_MODEL', 'GigaChat-2')}), облако"
    return "нейросеть не подключена — тексты будут заглушками"
