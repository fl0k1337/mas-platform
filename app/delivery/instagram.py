"""
Канал Instagram — ЗАГОТОВКА, ПО УМОЛЧАНИЮ ОТКЛЮЧЕНА.

Технически публикация возможна: Instagram Graph API, двухшаговая схема
(создать контейнер с медиа → опубликовать). Ограничения самой платформы:
нужен бизнес-аккаунт, связанный с Facebook-страницей, проверенное приложение
Meta и ОБЯЗАТЕЛЬНО картинка или видео по публичной ссылке — текстовый пост
без медиа опубликовать нельзя.

Почему выключено по умолчанию: Meta признана в РФ экстремистской организацией,
и для продукта, ориентированного на российских клиентов, автопубликация в
Instagram — юридический риск, который должен принимать владелец бизнеса
осознанно, а не получать «в комплекте». Включается вручную: строка
INSTAGRAM_ENABLED=1 в .env.
"""

from __future__ import annotations

import os

from app.delivery.base import BLOCKED, DRAFT, Channel, SendResult

API = "https://graph.facebook.com/v21.0"


class InstagramChannel(Channel):
    key = "instagram"
    title = "Instagram"
    maturity = DRAFT if os.getenv("INSTAGRAM_ENABLED", "").strip() in ("1", "true", "yes") \
        else BLOCKED
    hint = ("выключен по умолчанию: юридический риск для РФ. Включение — "
            "INSTAGRAM_ENABLED=1 в .env, решение принимает владелец бизнеса. "
            "Также нужны бизнес-аккаунт, токен Meta и медиафайл по ссылке")
    fields = [("ig_user_id", "ID бизнес-аккаунта Instagram"),
              ("access_token", "долгоживущий токен Meta")]

    def configured(self) -> bool:
        return bool(self.creds.get("ig_user_id") and self.creds.get("access_token"))

    def _send(self, text: str, image_url: str | None = None, **kw) -> SendResult:
        if not image_url:
            return SendResult(False, "Instagram: нужен медиафайл — "
                                     "публикация без картинки невозможна")
        import httpx
        uid, token = self.creds["ig_user_id"], self.creds["access_token"]
        # шаг 1: контейнер
        r1 = httpx.post(f"{API}/{uid}/media",
                        params={"image_url": image_url, "caption": text,
                                "access_token": token}, timeout=30)
        if r1.status_code >= 300:
            return SendResult(False, f"Instagram (контейнер): {r1.text[:200]}")
        creation_id = r1.json().get("id")
        # шаг 2: публикация
        r2 = httpx.post(f"{API}/{uid}/media_publish",
                        params={"creation_id": creation_id, "access_token": token},
                        timeout=30)
        if r2.status_code >= 300:
            return SendResult(False, f"Instagram (публикация): {r2.text[:200]}")
        return SendResult(True, "опубликовано в Instagram", str(r2.json().get("id")))
