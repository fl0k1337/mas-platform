"""
Финансовый контролёр: сверка «сметы ↔ фактические сделки CRM».

Принцип из спецификации: деньги сверяет ДЕТЕРМИНИРОВАННЫЙ код (никаких
галлюцинаций), а LLM лишь пишет пояснительную записку по найденным
расхождениям — что проверить руками в первую очередь. Агент ничего
не «чинит» сам, только подсвечивает.

Данные пока тестовые. В боевой версии:
  - сметы  -> Google Sheets клиента (gspread) или его учётная система;
  - сделки -> CRMAdapter.get_deals(stage=WON) из Integration Layer.
"""

from __future__ import annotations

from app import db, tg
from app.llm import build_llm

TOLERANCE_PCT = 5.0   # расхождение до 5% считаем нормой (скидки, округления)

SYSTEM_PROMPT = """\
Ты — финансовый аналитик компании ({industry}).
Тебе передают результат алгоритмической сверки смет и закрытых сделок CRM.
Напиши короткую пояснительную записку для руководителя (до 12 строк):
что проверить в первую очередь и почему, где возможные причины расхождений
(скидка без документа, недозаполненная CRM, потерянная оплата).
Используй только переданные данные, ничего не выдумывай. Русский язык.
"""


# ------------------------------------------------------ тестовые данные ---

def fetch_estimates() -> list[dict]:
    """MOCK смет (в бою — из Google Sheets клиента)."""
    return [
        {"id": "СМ-101", "deal_id": "D-501", "client": "ООО «Альфа»", "amount": 250_000},
        {"id": "СМ-102", "deal_id": "D-502", "client": "ИП Смирнов", "amount": 180_000},
        {"id": "СМ-103", "deal_id": "D-503", "client": "ООО «Гамма»", "amount": 95_000},
        {"id": "СМ-105", "deal_id": "D-505", "client": "ООО «Дельта»", "amount": 60_000},
    ]


def fetch_won_deals() -> list[dict]:
    """MOCK закрытых сделок CRM (в бою — CRMAdapter.get_deals)."""
    return [
        {"id": "D-501", "client": "ООО «Альфа»", "amount": 250_000, "resp": "Иванова А."},
        {"id": "D-502", "client": "ИП Смирнов", "amount": 150_000, "resp": "Петров К."},
        # D-503 в CRM не закрыта — смета есть, оплаты нет
        {"id": "D-504", "client": "ООО «Бета»", "amount": 320_000, "resp": "Петров К."},  # без сметы!
        {"id": "D-505", "client": "ООО «Дельта»", "amount": 61_000, "resp": "Иванова А."},
    ]


# ----------------------------------------------------------- сверка -------

def reconcile(estimates: list[dict], deals: list[dict]) -> tuple[list[str], int]:
    """Возвращает (список расхождений по убыванию важности, сколько сошлось)."""
    deals_by_id = {d["id"]: d for d in deals}
    matched_deal_ids: set[str] = set()
    mismatches: list[str] = []
    ok = 0

    for est in estimates:
        deal = deals_by_id.get(est["deal_id"])
        if deal is None:
            mismatches.append(
                f"🚨 Смета {est['id']} ({est['client']}, {est['amount']:,} ₽) — "
                f"закрытой сделки в CRM НЕТ: оплата не зафиксирована или сделка не закрыта")
            continue
        matched_deal_ids.add(deal["id"])
        diff = deal["amount"] - est["amount"]
        diff_pct = diff / est["amount"] * 100
        if abs(diff_pct) <= TOLERANCE_PCT:
            ok += 1
        else:
            mismatches.append(
                f"⚠ {est['client']}: смета {est['id']} = {est['amount']:,} ₽, "
                f"сделка {deal['id']} = {deal['amount']:,} ₽ "
                f"({diff:+,} ₽, {diff_pct:+.1f}%) — отв. {deal['resp']}")

    for deal in deals:
        if deal["id"] not in matched_deal_ids:
            mismatches.append(
                f"❓ Сделка {deal['id']} ({deal['client']}, {deal['amount']:,} ₽, "
                f"отв. {deal['resp']}) закрыта БЕЗ сметы — проверить основание суммы")

    # критичное (🚨) — первым
    order = {"🚨": 0, "⚠": 1, "❓": 2}
    mismatches.sort(key=lambda s: order.get(s[0], 3))
    return mismatches, ok


def run(tenant_id: int) -> str:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return "тенант не найден"
    run_id = db.start_run(tenant_id, "finance_check")
    try:
        estimates, deals = fetch_estimates(), fetch_won_deals()
        mismatches, ok = reconcile(estimates, deals)

        header = (f"💰 Финансовая сверка — {tenant['name']}\n"
                  f"Смет: {len(estimates)}, закрытых сделок: {len(deals)}, "
                  f"сошлось: {ok}, расхождений: {len(mismatches)}\n")
        body = "\n".join(mismatches) if mismatches else "Все сметы сошлись со сделками 🎉"

        note = ""
        if mismatches:
            llm = build_llm(temperature=0.2)
            if llm is not None:
                note = "\n\n📋 Пояснительная записка:\n" + llm.invoke([
                    ("system", SYSTEM_PROMPT.format(industry=tenant["industry"])),
                    ("user", header + body),
                ]).content

        report = header + "\n" + body + note
        tg.notify(report)
        db.finish_run(run_id, "done", report)
        return f"сошлось: {ok}, расхождений: {len(mismatches)}"
    except Exception as e:
        db.finish_run(run_id, "failed", str(e))
        raise
