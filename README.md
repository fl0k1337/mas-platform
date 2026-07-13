# MAS Platform — мультиагентная автоматизация маркетинга

B2B SaaS: ИИ-агенты для маркетинговых и операционных задач
(отчёты по трафику, контроль лидов Calltouch↔CRM, контент, рассылки).

## Прототипы
- `traffic_report.py` — еженедельный отчёт по трафику (FastAPI + LangGraph + Ollama), доставка в Telegram
- `lead_control.py` — ежедневная сверка лидов Calltouch↔CRM (детерминированный алгоритм)

## Запуск
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
скопировать .env.example в .env и заполнить токены