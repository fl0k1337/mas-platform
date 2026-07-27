"""Диагностика ключа GigaChat. Запуск:  python check_gigachat.py
Печатает только длину и первые символы ключа (не весь), безопасно."""

import base64
import os
import uuid

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

key = (os.getenv("GIGACHAT_CREDENTIALS") or "").strip()

print("=" * 50)
if not key:
    raise SystemExit("В .env нет GIGACHAT_CREDENTIALS (или пусто).")

print(f"Длина ключа: {len(key)} символов")
print(f"Начало: {key[:8]}…  конец: …{key[-6:]}")
if key != os.getenv("GIGACHAT_CREDENTIALS"):
    print("⚠ Вокруг ключа были пробелы/переносы — они убраны при чтении, "
          "но лучше поправить .env.")

# 1) Правильный ли это Authorization Key (base64 от 'client_id:client_secret')?
print("-" * 50)
try:
    decoded = base64.b64decode(key).decode("utf-8", "replace")
    parts = decoded.split(":")
    def _is_uuid(s):
        try:
            uuid.UUID(s.strip()); return True
        except ValueError:
            return False
    if len(parts) == 2 and _is_uuid(parts[0]) and _is_uuid(parts[1]):
        print("✅ Формат ключа ВЕРНЫЙ (это Authorization Key = client_id:client_secret).")
        print("   Значит проблема не в формате, а в том, что ключ устарел/отозван —")
        print("   сгенерируйте НОВЫЙ Authorization Key и обновите .env.")
    else:
        print("❌ Это НЕ Authorization Key. Похоже, скопирована не та строка")
        print("   (например, Client Secret или Client ID по отдельности).")
        print("   Нужен именно 'Ключ авторизации / Authorization Key' целиком.")
except Exception:
    print("❌ Строка не является корректным base64 — скопирована не та строка")
    print("   или ключ обрезан. Возьмите Authorization Key целиком.")

# 2) Пробуем реальную авторизацию
print("-" * 50)
print("Пробую авторизацию в Сбере…")
try:
    import httpx
    r = httpx.post(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        headers={"Authorization": f"Basic {key}",
                 "RqUID": str(uuid.uuid4()),
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"scope": os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")},
        timeout=20, verify=False,
    )
    if r.status_code == 200:
        print("✅ УСПЕХ! Токен получен, ключ рабочий. Можно пользоваться GigaChat.")
    else:
        print(f"❌ Ответ {r.status_code}: {r.text}")
except Exception as e:
    print(f"Ошибка запроса: {e}")
print("=" * 50)