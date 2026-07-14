"""
Планировщик v0.2 — работает с новой структурой продукта.

Отличия от прототипа poc/scheduler.py:
  - агенты вызываются напрямую как функции (без subprocess — и без проблем
    с кодировками Windows);
  - задачи выполняются ДЛЯ КАЖДОГО клиента из базы — добавили тенанта
    в панели, и он автоматически попал в расписание. Это и есть
    мультитенантность в действии;
  - каждый запуск фиксируется в agent_runs — виден в веб-панели.

Запуск:  python scheduler.py   (TEST_MODE=True — быстрый прогон для проверки)
"""

from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from app import db
from app.agents import content, finance, leads, traffic

TEST_MODE = True   # True: всё запускается через 1-2 минуты; False: боевое расписание

SCHEDULE = {
    "leads":   {"cron": {"hour": 9, "minute": 0},                     "fn": leads.run},
    "traffic": {"cron": {"day_of_week": "mon", "hour": 9, "minute": 30}, "fn": traffic.run},
    "finance": {"cron": {"day_of_week": "tue", "hour": 10, "minute": 0}, "fn": finance.run},
    "content": {"cron": {"day_of_week": "fri", "hour": 10, "minute": 0}, "fn": content.run},
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
