"""
Планировщик v0.2 — работает с новой структурой продукта.

Отличия от прототипа poc/scheduler.py:
  - агенты вызываются напрямую как функции (без subprocess — и без проблем
    с кодировками Windows);
  - задачи выполняются ДЛЯ КАЖДОГО клиента из базы — добавили тенанта
    в панели, и он автоматически попал в расписание. Это и есть
    мультитенантность в действии;
  - каждый запуск фиксируется в agent_runs — виден в веб-панели.

Режим по умолчанию — БОЕВОЙ (расписание по времени). Тестовый режим (задачи
раз в 1-3 минуты для быстрой проверки) включается ТОЛЬКО явно: строкой
SCHEDULER_TEST=1 в .env или переменной окружения. Так «забыть выключить тест»
на сервере невозможно.

Запуск:  python scheduler.py
Тест:    SCHEDULER_TEST=1 python scheduler.py   (или строка SCHEDULER_TEST=1 в .env)
"""

from __future__ import annotations

import os
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app import db
from app.agents import (competitors, content, content_plan, finance, leads,
                        mailings, traffic, utp)

# Боевой режим по умолчанию. Тест включается только явным SCHEDULER_TEST=1.
TEST_MODE = os.getenv("SCHEDULER_TEST", "").strip() in ("1", "true", "yes", "on")

SCHEDULE = {
    "leads":   {"cron": {"hour": 9, "minute": 0},                     "fn": leads.run},
    "traffic": {"cron": {"day_of_week": "mon", "hour": 9, "minute": 30}, "fn": traffic.run},
    "finance": {"cron": {"day_of_week": "tue", "hour": 10, "minute": 0}, "fn": finance.run},
    "content": {"cron": {"day_of_week": "fri", "hour": 10, "minute": 0}, "fn": content.run},
    "mailings": {"cron": {"day_of_week": "fri", "hour": 11, "minute": 0}, "fn": mailings.run},
    "competitors": {"cron": {"day": "1", "hour": 9, "minute": 0}, "fn": competitors.run},  # 1-е число месяца
    "utp": {"cron": {"day": "1", "hour": 11, "minute": 0}, "fn": utp.run},                  # 1-е число месяца
    # план — 2-го числа, чтобы опереться на свежий анализ конкурентов от 1-го
    "content_plan": {"cron": {"day": "2", "hour": 9, "minute": 0}, "fn": content_plan.run},
}


def for_all_tenants(job_name: str, fn) -> None:
    """Выполнить задачу для каждого клиента платформы."""
    tenants = db.list_tenants()
    print(f"\n[{datetime.now():%d.%m %H:%M:%S}] ▶ {job_name}: клиентов в базе — {len(tenants)}")
    for t in tenants:
        try:
            result = fn(t["id"])
            print(f"   ✔ {t['name']}: {result}")
        except Exception as e:  # ошибка одного клиента не роняет остальных
            print(f"   ⚠ {t['name']}: ошибка — {e}")


if __name__ == "__main__":
    db.init_db()
    scheduler = BlockingScheduler()

    for name, cfg in SCHEDULE.items():
        trigger = ({"trigger": "interval", "minutes": 1 + list(SCHEDULE).index(name)}
                   if TEST_MODE else {"trigger": "cron", **cfg["cron"]})
        scheduler.add_job(for_all_tenants, args=[name, cfg["fn"]],
                          id=name, max_instances=1, **trigger)

    mode = "ТЕСТОВОМ (задачи через 1-3 мин)" if TEST_MODE else \
           "боевом (лиды 09:00 ежедн., трафик пн 09:30, контент пт 10:00)"
    print(f"Планировщик запущен в {mode} режиме. Остановка: Ctrl+C")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nПланировщик остановлен.")
