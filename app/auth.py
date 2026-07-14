"""
Авторизация: хэширование паролей и сессионные токены.

Всё на стандартной библиотеке — без внешних зависимостей:
  - пароли НИКОГДА не хранятся в открытом виде. В базе лежит только хэш
    PBKDF2-SHA256 с индивидуальной солью — восстановить пароль из него нельзя;
  - сессия — cookie вида "user_id.timestamp.подпись". Подпись — HMAC от
    секретного ключа сервера: подделать cookie, не зная ключа, невозможно.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from app.config import SECRET_KEY

PBKDF2_ITERATIONS = 200_000
SESSION_MAX_AGE = 60 * 60 * 24 * 14   # сессия живёт 14 дней


# ---------------------------------------------------------------- пароли ---

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                 PBKDF2_ITERATIONS).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                PBKDF2_ITERATIONS).hex()
    return hmac.compare_digest(check, digest)   # сравнение без утечки по времени


# ----------------------------------------------------------------- сессии ---

def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time())}"
    return f"{payload}.{_sign(payload)}"


def verify_session_token(token: str | None) -> int | None:
    """Вернёт user_id, если cookie подлинная и не протухла, иначе None."""
    if not token:
        return None
    try:
        user_id, ts, signature = token.split(".")
        payload = f"{user_id}.{ts}"
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    if time.time() - int(ts) > SESSION_MAX_AGE:
        return None
    return int(user_id)
