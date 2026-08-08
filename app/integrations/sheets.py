"""
Чтение смет из Google Таблицы клиента.

Модель доступа: у платформы ОДИН сервисный аккаунт (google_key.json в корне
проекта). Клиент делится своей таблицей смет с email этого аккаунта — и всё,
никаких OAuth. Тот же сервисный аккаунт потом используется для УТП и ТЗ дизайнеру.

Ожидаемый формат таблицы: первая строка — заголовки. Колонки распознаются по
смыслу (не по точному имени), поэтому подходят разные шаблоны клиентов:
  • «ID сделки» / «Сделка» / «Deal»      → id сделки в CRM (для сопоставления)
  • «Клиент» / «Компания»                → название (справочно и как запасной ключ)
  • «Сумма сметы» / «План» / «Смета»     → плановая сумма

Разбор строк (_rows_to_estimates) отделён от сети — тестируется на фикстурах.
"""

from __future__ import annotations

from app.config import BASE_DIR

KEY_FILE = BASE_DIR / "google_key.json"


def _pick(record: dict, *keywords: str):
    """Первое значение, чей заголовок содержит одно из ключевых слов."""
    for key, val in record.items():
        kl = str(key).lower()
        if any(w in kl for w in keywords):
            return val
    return None


def _to_amount(val) -> float | None:
    if val in (None, ""):
        return None
    s = str(val).replace(" ", "").replace(" ", "").replace(",", ".")
    s = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _rows_to_estimates(records: list[dict]) -> list[dict]:
    """list[dict] (строки таблицы с заголовками) -> список смет."""
    out = []
    for r in records:
        deal_id = _pick(r, "сделк", "deal")
        client = _pick(r, "клиент", "компан")
        planned = _to_amount(_pick(r, "смет", "план", "сумма", "amount"))
        if planned is None and not deal_id:
            continue                      # пустая/служебная строка
        out.append({
            "deal_id": str(deal_id).strip() if deal_id else None,
            "client": str(client).strip() if client else None,
            "planned": planned,
        })
    return out


def read_estimates(sheet_id: str, worksheet: str | None = None) -> list[dict]:
    """Прочитать сметы из Google Таблицы. Требует google_key.json."""
    import gspread
    gc = gspread.service_account(filename=str(KEY_FILE))
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet) if worksheet else sh.sheet1
    return _rows_to_estimates(ws.get_all_records())


def write_table(sheet_id: str, title: str, header: list[str],
                rows: list[list[str]]) -> str:
    """Создать в таблице новый лист и записать в него таблицу.
    Возвращает ссылку на лист. Требует google_key.json и права «Редактор»."""
    import gspread
    gc = gspread.service_account(filename=str(KEY_FILE))
    sh = gc.open_by_key(sheet_id)

    # имя листа уникальное: если такое уже есть, добавляем счётчик
    base, name, n = title[:90], title[:90], 2
    existing = {ws.title for ws in sh.worksheets()}
    while name in existing:
        name = f"{base} ({n})"
        n += 1

    ws = sh.add_worksheet(title=name, rows=max(len(rows) + 10, 50),
                          cols=max(len(header), 10))
    ws.append_rows([header] + rows, value_input_option="RAW")
    try:
        ws.format(f"A1:{chr(64 + len(header))}1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
    except Exception:
        pass                       # оформление необязательно, данные важнее
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"


def sheet_title(sheet_id: str) -> str:
    """Название таблицы — для проверки доступа при подключении."""
    import gspread
    gc = gspread.service_account(filename=str(KEY_FILE))
    return gc.open_by_key(sheet_id).title
