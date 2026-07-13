"""
Планировщик задач — локальная версия Celery Beat из архитектурной спецификации.

Держит расписание и в нужный момент запускает скрипты-задачи:
  - lead_control.py    -> ежедневно в 09:00 (контроль лидов)
  - traffic_report.py  -> по понедельникам в 09:30 (отчёт по трафику)

Запуск:
    pip install apscheduler
    python scheduler.py

Пока это окно терминала открыто (и компьютер не спит) — задачи будут
выполняться сами и присылать отчёты в Telegram.
Остановить: Ctrl + C.

ВАЖНО: для первой проверки стоит TEST_MODE = True — обе задачи запустятся
через 1-2 минуты после старта, чтобы вы сразу увидели, что всё работает.
Убедились — поставьте TEST_MODE = False и перезапустите: планировщик
перейдёт на боевое расписание.
"""

from __future__ import annotations

import subprocess
import os
import sys
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

BASE_DIR = Path(__file__).parent   # папка, где лежат скрипты-задачи

TEST_MODE = True   # True = тестовый прогон каждые пару минут; False = боевое расписание

# Боевое расписание (время — местное, по часам вашего компьютера)
LEAD_CONTROL_AT = {"hour": 9, "minute": 0}                        # ежедневно 09:00
TRAFFIC_REPORT_AT = {"day_of_week": "mon", "hour": 9, "minute": 30}  # понедельник 09:30


def run_script(filename: str) -> None:
    """Запускает скрипт-задачу тем же Python, что и планировщик (из .venv),
    и показывает его вывод. Ошибка задачи не роняет сам планировщик."""
    print(f"\n[{datetime.now():%d.%m %H:%M:%S}] ▶ запускаю {filename} ...")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / filename)],
        capture_output=True, text=True, encoding="utf-8",
        env=env,  # заставляем дочерний скрипт печатать в UTF-8, а не в cp1251
    )
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        print(f"⚠ {filename} завершился с ошибкой:\n{result.stderr}")
    else:
        print(f"[{datetime.now():%d.%m %H:%M:%S}] ✔ {filename} выполнен")


def job_lead_control() -> None:
    run_script("lead_control.py")


def job_traffic_report() -> None:
    run_script("traffic_report.py")


if __name__ == "__main__":
    scheduler = BlockingScheduler()

    if TEST_MODE:
        # Тест: сверка лидов через минуту, отчёт по трафику через две
        scheduler.add_job(job_lead_control, "interval", minutes=1,
                          id="lead_control", max_instances=1)
        scheduler.add_job(job_traffic_report, "interval", minutes=2,
                          id="traffic_report", max_instances=1)
        print("Планировщик запущен в ТЕСТОВОМ режиме:")
        print("  - контроль лидов: каждую минуту")
        print("  - отчёт по трафику: каждые 2 минуты (LLM думает ~1-3 мин, наберитесь терпения)")
        print("Проверьте Telegram, затем поставьте TEST_MODE = False и перезапустите.")
    else:
        scheduler.add_job(job_lead_control, "cron", id="lead_control",
                          max_instances=1, **LEAD_CONTROL_AT)
        scheduler.add_job(job_traffic_report, "cron", id="traffic_report",
                          max_instances=1, **TRAFFIC_REPORT_AT)
        print("Планировщик запущен в боевом режиме:")
        print("  - контроль лидов: ежедневно в 09:00")
        print("  - отчёт по трафику: по понедельникам в 09:30")

    print("Окно можно свернуть. Остановка: Ctrl + C\n")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nПланировщик остановлен.")