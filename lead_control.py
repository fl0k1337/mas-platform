"""
Прототип №2: ежедневный контроль лидов Calltouch -> CRM.

Что делает (полностью детерминированно, без LLM — по принципу из спецификации):
  1. Берёт обращения за сутки из Calltouch (звонки и заявки) — пока тестовые данные.
  2. Нормализует телефоны к формату E.164 (+79251112233), какими бы кривыми они ни были.
  3. Ищет для каждого обращения лид в CRM по телефону (окно ±24 часа).
  4. Проверяет, обработан ли лид менеджером (SLA: статус должен смениться с NEW за 4 часа).
  5. Выносит вердикты:
       matched_ok             — лид есть и обработан;
       matched_not_processed  — лид есть, но менеджер не взял его в работу вовремя;
       missing_in_crm         — обращение было, а лида в CRM НЕТ (потерянные деньги!);
       unmatchable            — скрытый/кривой номер, сверить невозможно;
       skipped_non_target     — нецелевое обращение (спам, ошиблись номером), не сверяем.
  6. Печатает отчёт и отправляет его в Telegram (тот же .env, что и у traffic_report).

Запуск:
    pip install phonenumbers        (рекомендуется; без неё сработает упрощённый разбор номеров)
    python lead_control.py

Где здесь место для реального API (когда дойдём до боевого подключения):
  - fetch_calltouch_interactions() -> POST https://api.calltouch.ru/calls-service/RestAPI/{node}/calls-diary/calls
                                      и журнал заявок (requests) — методы из справки Calltouch;
  - fetch_crm_leads()              -> CRMAdapter.get_leads()/find_by_phone() из Integration Layer
                                      (amoCRM: GET /api/v4/leads; Bitrix24: crm.lead.list).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, timedelta

from pydantic import BaseModel

# .env с TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — как в traffic_report.py
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # без python-dotenv просто не будет Telegram-уведомления

# phonenumbers — «взрослая» библиотека нормализации номеров (используется во всех CRM мира)
try:
    import phonenumbers
    HAS_PHONENUMBERS = True
except ImportError:
    HAS_PHONENUMBERS = False

# ----------------------------------------------------------------------------
# НАСТРОЙКИ СВЕРКИ (в проде — settings интеграции per-tenant из БД)
# ----------------------------------------------------------------------------

SLA_HOURS = 4            # за сколько часов менеджер обязан взять лид в работу
MATCH_WINDOW_HOURS = 24  # в каком окне вокруг обращения ищем лид в CRM

NOW = datetime.now()     # точка отсчёта; в проде — время запуска задачи Celery Beat


# ----------------------------------------------------------------------------
# 1. МОДЕЛИ ДАННЫХ (упрощённые unified-модели из спецификации)
# ----------------------------------------------------------------------------

class CTInteraction(BaseModel):
    """Обращение из Calltouch: звонок или заявка с сайта."""
    external_id: str
    kind: str                      # call | request
    phone_raw: str | None          # телефон «как есть» — форматы бывают любые
    is_target: bool                # целевое ли обращение (флаг Calltouch)
    duration_sec: int | None = None
    occurred_at: datetime


class CRMLead(BaseModel):
    """Лид из CRM (после нормализации адаптером)."""
    external_id: str
    phone_raw: str | None
    status: str                    # unified: NEW | IN_PROGRESS | QUALIFIED | REJECTED
    responsible: str               # менеджер
    created_at: datetime


class Verdict(BaseModel):
    interaction_id: str
    kind: str
    phone_e164: str | None
    verdict: str
    lead_id: str | None = None
    responsible: str | None = None
    note: str = ""


# ----------------------------------------------------------------------------
# 2. НОРМАЛИЗАЦИЯ ТЕЛЕФОНОВ — сердце сверки
# ----------------------------------------------------------------------------

def normalize_phone(raw: str | None) -> str | None:
    """Приводит «8 (925) 111-22-33», «+7 925 111 22 33», «89251112233»
    к единому виду +79251112233. Возвращает None, если номер не разобрать."""
    if not raw or "скрыт" in raw.lower() or raw.strip() in ("", "-"):
        return None

    if HAS_PHONENUMBERS:
        try:
            parsed = phonenumbers.parse(raw, "RU")
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
        return None

    # Упрощённый разбор для РФ, если библиотека не установлена
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


# ----------------------------------------------------------------------------
# 3. ТЕСТОВЫЕ ДАННЫЕ (в проде эти две функции заменяются вызовами API)
# ----------------------------------------------------------------------------

def fetch_calltouch_interactions() -> list[CTInteraction]:
    """MOCK журнала Calltouch за вчера. Обратите внимание на разные форматы номеров."""
    h = lambda hours: NOW - timedelta(hours=hours)
    return [
        # 1) Целевой звонок, лид в CRM есть и обработан -> matched_ok
        CTInteraction(external_id="ct-101", kind="call", phone_raw="8 (925) 111-22-33",
                      is_target=True, duration_sec=240, occurred_at=h(20)),
        # 2) Целевой звонок, лид создан, но менеджер так и не взял в работу -> matched_not_processed
        CTInteraction(external_id="ct-102", kind="call", phone_raw="+7 916 222-33-44",
                      is_target=True, duration_sec=180, occurred_at=h(19)),
        # 3) Целевой звонок, лида в CRM НЕТ вообще -> missing_in_crm (потерянный клиент!)
        CTInteraction(external_id="ct-103", kind="call", phone_raw="89032223355",
                      is_target=True, duration_sec=310, occurred_at=h(18)),
        # 4) Заявка с сайта, лид есть и обработан -> matched_ok
        CTInteraction(external_id="ct-104", kind="request", phone_raw="+7(999)444-55-66",
                      is_target=True, occurred_at=h(16)),
        # 5) Звонок со скрытым номером -> unmatchable (в отчёт, но без паники)
        CTInteraction(external_id="ct-105", kind="call", phone_raw="номер скрыт",
                      is_target=True, duration_sec=95, occurred_at=h(15)),
        # 6) Спам-звонок 8 секунд, Calltouch пометил нецелевым -> skipped_non_target
        CTInteraction(external_id="ct-106", kind="call", phone_raw="84950000001",
                      is_target=False, duration_sec=8, occurred_at=h(14)),
        # 7) Повторный звонок клиента №1 тем же вечером -> должен приматчиться к тому же лиду
        CTInteraction(external_id="ct-107", kind="call", phone_raw="+79251112233",
                      is_target=True, duration_sec=60, occurred_at=h(10)),
    ]


def fetch_crm_leads() -> list[CRMLead]:
    """MOCK лидов из CRM за последние двое суток. Форматы номеров снова разные —
    нормализация должна «склеить» их с журналом Calltouch."""
    h = lambda hours: NOW - timedelta(hours=hours)
    return [
        CRMLead(external_id="crm-9001", phone_raw="+7 925 111 22 33",
                status="IN_PROGRESS", responsible="Иванова А.", created_at=h(20)),
        CRMLead(external_id="crm-9002", phone_raw="8-916-222-33-44",
                status="NEW", responsible="Петров К.", created_at=h(19)),   # висит в NEW дольше SLA!
        CRMLead(external_id="crm-9003", phone_raw="9994445566",
                status="QUALIFIED", responsible="Иванова А.", created_at=h(15)),
        # Лид не из рекламы (пришёл по сарафану) — в сверке участвовать не должен
        CRMLead(external_id="crm-9004", phone_raw="+7 911 777 88 99",
                status="NEW", responsible="Петров К.", created_at=h(5)),
    ]


# ----------------------------------------------------------------------------
# 4. АЛГОРИТМ СВЕРКИ
# ----------------------------------------------------------------------------

def reconcile(interactions: list[CTInteraction], leads: list[CRMLead]) -> list[Verdict]:
    # Индекс лидов по нормализованному телефону: телефон -> список лидов
    leads_by_phone: dict[str, list[CRMLead]] = {}
    for lead in leads:
        e164 = normalize_phone(lead.phone_raw)
        if e164:
            leads_by_phone.setdefault(e164, []).append(lead)

    verdicts: list[Verdict] = []
    window = timedelta(hours=MATCH_WINDOW_HOURS)
    sla = timedelta(hours=SLA_HOURS)

    for it in interactions:
        if not it.is_target:
            verdicts.append(Verdict(interaction_id=it.external_id, kind=it.kind,
                                    phone_e164=normalize_phone(it.phone_raw),
                                    verdict="skipped_non_target",
                                    note="нецелевое обращение (фильтр Calltouch)"))
            continue

        e164 = normalize_phone(it.phone_raw)
        if e164 is None:
            verdicts.append(Verdict(interaction_id=it.external_id, kind=it.kind,
                                    phone_e164=None, verdict="unmatchable",
                                    note=f"номер не распознан: {it.phone_raw!r}"))
            continue

        # Кандидаты: лиды с тем же номером в окне ±24 часа от обращения
        candidates = [l for l in leads_by_phone.get(e164, [])
                      if abs(l.created_at - it.occurred_at) <= window]
        if not candidates:
            verdicts.append(Verdict(interaction_id=it.external_id, kind=it.kind,
                                    phone_e164=e164, verdict="missing_in_crm",
                                    note="обращение есть в Calltouch, лида в CRM нет"))
            continue

        # Ближайший по времени лид (обрабатывает и повторные звонки клиента)
        lead = min(candidates, key=lambda l: abs(l.created_at - it.occurred_at))
        overdue = lead.status == "NEW" and (NOW - lead.created_at) > sla
        verdicts.append(Verdict(
            interaction_id=it.external_id, kind=it.kind, phone_e164=e164,
            verdict="matched_not_processed" if overdue else "matched_ok",
            lead_id=lead.external_id, responsible=lead.responsible,
            note=(f"лид висит в NEW дольше {SLA_HOURS} ч" if overdue else ""),
        ))
    return verdicts


# ----------------------------------------------------------------------------
# 5. ОТЧЁТ И ДОСТАВКА
# ----------------------------------------------------------------------------

EMOJI = {"matched_ok": "✅", "matched_not_processed": "⏰",
         "missing_in_crm": "🚨", "unmatchable": "❓", "skipped_non_target": "🗑"}

TITLES = {"matched_ok": "Обработано вовремя",
          "matched_not_processed": "НЕ ВЗЯТО В РАБОТУ (нарушен SLA)",
          "missing_in_crm": "ПОТЕРЯНО — нет лида в CRM",
          "unmatchable": "Несверяемые (скрытый/кривой номер)",
          "skipped_non_target": "Нецелевые (не сверяем)"}


def build_report(verdicts: list[Verdict]) -> str:
    counts = Counter(v.verdict for v in verdicts)
    lines = [f"🔎 Контроль лидов Calltouch → CRM за {NOW:%d.%m.%Y}",
             f"Всего обращений: {len(verdicts)}", ""]

    for key in ("missing_in_crm", "matched_not_processed", "matched_ok",
                "unmatchable", "skipped_non_target"):
        if counts.get(key):
            lines.append(f"{EMOJI[key]} {TITLES[key]}: {counts[key]}")
    lines.append("")

    problems = [v for v in verdicts if v.verdict in ("missing_in_crm", "matched_not_processed")]
    if problems:
        lines.append("Требуют действий сегодня:")
        for v in problems:
            who = f", отв. {v.responsible}" if v.responsible else ""
            lead = f", лид {v.lead_id}" if v.lead_id else ""
            lines.append(f"{EMOJI[v.verdict]} {v.kind} {v.interaction_id}, "
                         f"{v.phone_e164}{lead}{who} — {v.note}")
        lines.append("")
        lines.append("В боевой версии по каждой строке автоматически создаётся "
                     "задача в CRM на ответственного (CRMAdapter.create_task).")
    else:
        lines.append("Проблем не найдено — все обращения дошли до CRM и обработаны. 🎉")

    return "\n".join(lines)


def send_telegram(text: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return "skipped: нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в .env"
    try:
        import httpx
        for i in range(0, len(text), 4000):
            resp = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": text[i:i + 4000]},
                              timeout=15)
            resp.raise_for_status()
        return "sent"
    except Exception as e:
        return f"error: {e}"


# ----------------------------------------------------------------------------

if __name__ == "__main__":
    if not HAS_PHONENUMBERS:
        print("(подсказка: pip install phonenumbers — включит строгую проверку номеров)\n")
    interactions = fetch_calltouch_interactions()
    leads = fetch_crm_leads()
    verdicts = reconcile(interactions, leads)
    report = build_report(verdicts)
    print(report)
    print("\nTelegram:", send_telegram(report))