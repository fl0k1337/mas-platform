"""Контролёр лидов Calltouch↔CRM. Полностью детерминированный (без LLM).
Данные пока тестовые — при боевом подключении меняются только fetch_*."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from app import db, tg

SLA_HOURS = 4
WINDOW_HOURS = 24

try:
    import phonenumbers
    HAS_PN = True
except ImportError:
    HAS_PN = False


def normalize_phone(raw: str | None) -> str | None:
    if not raw or "скрыт" in raw.lower():
        return None
    if HAS_PN:
        try:
            p = phonenumbers.parse(raw, "RU")
            if phonenumbers.is_valid_number(p):
                return phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


def fetch_calltouch(now: datetime) -> list[dict]:
    h = lambda x: now - timedelta(hours=x)
    return [
        {"id": "ct-101", "phone": "8 (925) 111-22-33", "target": True, "at": h(20)},
        {"id": "ct-102", "phone": "+7 916 222-33-44", "target": True, "at": h(19)},
        {"id": "ct-103", "phone": "89032223355", "target": True, "at": h(18)},
        {"id": "ct-105", "phone": "номер скрыт", "target": True, "at": h(15)},
        {"id": "ct-106", "phone": "84950000001", "target": False, "at": h(14)},
    ]


def fetch_crm_leads(now: datetime) -> list[dict]:
    h = lambda x: now - timedelta(hours=x)
    return [
        {"id": "crm-9001", "phone": "+7 925 111 22 33", "status": "IN_PROGRESS",
         "resp": "Иванова А.", "at": h(20)},
        {"id": "crm-9002", "phone": "8-916-222-33-44", "status": "NEW",
         "resp": "Петров К.", "at": h(19)},
    ]


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    run_id = db.start_run(tenant_id, "lead_control")
    try:
        now = datetime.now()
        interactions = fetch_calltouch(now)
        leads = fetch_crm_leads(now)

        by_phone: dict[str, list[dict]] = {}
        for lead in leads:
            e164 = normalize_phone(lead["phone"])
            if e164:
                by_phone.setdefault(e164, []).append(lead)

        problems, ok = [], 0
        for it in interactions:
            if not it["target"]:
                continue
            e164 = normalize_phone(it["phone"])
            if e164 is None:
                problems.append(f"❓ {it['id']}: номер не распознан ({it['phone']})")
                continue
            cands = [l for l in by_phone.get(e164, [])
                     if abs(l["at"] - it["at"]) <= timedelta(hours=WINDOW_HOURS)]
            if not cands:
                problems.append(f"🚨 {it['id']}: {e164} — лида в CRM нет")
                continue
            lead = min(cands, key=lambda l: abs(l["at"] - it["at"]))
            if lead["status"] == "NEW" and (now - lead["at"]) > timedelta(hours=SLA_HOURS):
                problems.append(f"⏰ {it['id']}: {e164} — лид {lead['id']} висит в NEW "
                                f"дольше {SLA_HOURS} ч (отв. {lead['resp']})")
            else:
                ok += 1

        report = (f"🔎 Контроль лидов — {tenant['name']} за {now:%d.%m.%Y}\n"
                  f"✅ в порядке: {ok}\n" +
                  ("\n".join(problems) if problems else "Проблем нет 🎉"))
        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"в порядке: {ok}, проблем: {len(problems)}"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
