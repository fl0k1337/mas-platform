"""
Веб-панель платформы.

Страницы:
  /                — обзор: показатели, активность, клиенты, последние запуски
  /tenants/{id}    — карточка клиента: агенты, бренд, план, конкуренты, интеграции
  /tenants/{id}/stages — сопоставление стадий CRM клиента с нашими статусами
  /content         — очередь контента с кнопками согласования
  /runs            — журнал запусков, /runs/{id} — полный отчёт
  /credentials     — загрузка ключей (Google) через браузер

Запуск:  python run_web.py  ->  http://127.0.0.1:8000
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, db, tg, version
from app.agents import competitors as competitors_agent
from app.agents import content as content_agent
from app.agents import finance as finance_agent
from app.agents import leads as leads_agent
from app.agents import mailings as mailings_agent
from app.agents import traffic as traffic_agent
from app.agents import utp as utp_agent
from app.integrations import service as integrations_service

app = FastAPI(title="MAS Platform", version=version.VERSION)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

db.init_db()

# --------------------------------------------------------------------------
# Словари человеческих названий: оператор не должен видеть технические коды
# --------------------------------------------------------------------------

GRAPH_LABELS = {
    "content_weekly": "Контент недели",
    "traffic_report": "Отчёт по трафику",
    "lead_control": "Контроль лидов",
    "finance_check": "Финансовый отчёт",
    "mailings_weekly": "Рассылки SMS / WhatsApp",
    "competitors_monthly": "Анализ конкурентов",
    "crm_sync": "Проверка данных CRM",
    "utp_table": "Таблица УТП",
}
RUN_STATUS = {"running": "выполняется", "done": "готово", "failed": "ошибка"}
CONTENT_STATUS = {
    "pending_approval": "ждёт согласования", "needs_human": "нужна правка",
    "approved": "утверждено", "published": "опубликовано", "rejected": "отклонено",
}
INTEGRATION_LABELS = {
    "crm_bitrix24": "CRM Битрикс24", "crm_amocrm": "CRM amoCRM",
    "calltouch": "Calltouch (звонки)", "estimates_sheet": "Сметы (Google Таблица)",
    "utp_sheet": "УТП (Google Таблица)",
}
INTEGRATION_STATUS = {"active": "работает", "error": "ошибка", "pending": "не проверено"}

def fmt_dt(value: str | None, with_year: bool = False) -> str:
    """'2026-08-08 14:33:40' -> '08.08 14:33' (по-русски, без секунд)."""
    if not value:
        return "—"
    try:
        d, t = str(value).split(" ")
        y, m, day = d.split("-")
        stamp = f"{day}.{m}" + (f".{y[2:]}" if with_year else "")
        return f"{stamp} {t[:5]}"
    except Exception:
        return str(value)


templates.env.filters["dt"] = fmt_dt
templates.env.globals.update(
    glabel=lambda n: GRAPH_LABELS.get(n, n),
    srun=lambda s: RUN_STATUS.get(s, s),
    scontent=lambda s: CONTENT_STATUS.get(s, s),
    ilabel=lambda k: INTEGRATION_LABELS.get(k, k),
    istatus=lambda s: INTEGRATION_STATUS.get(s, s),
    app_version=version.VERSION,
    app_build=version.BUILD,
)

AGENTS = {
    "leads":       ("Контроль лидов",        leads_agent.run,       "Проверить, все ли лиды дошли до CRM и обработаны"),
    "traffic":     ("Отчёт по трафику",      traffic_agent.run,     "Показатели рекламы и аномалии за период"),
    "finance":     ("Финансовый отчёт",      finance_agent.run,     "Продажи из CRM и сверка со сметами"),
    "content":     ("Контент недели",        content_agent.run,     "Написать посты по контент-плану"),
    "mailings":    ("Рассылки",              mailings_agent.run,    "Тексты SMS и WhatsApp на неделю"),
    "competitors": ("Анализ конкурентов",    competitors_agent.run, "Проверить изменения на сайтах конкурентов"),
    "utp":         ("Таблица УТП",           utp_agent.run,         "Предложения по сегментам аудитории для рекламы"),
}
# Кнопка «запустить всё» гоняет только безопасные аналитические задачи:
# генерация контента и рассылок запускается осознанно, отдельной кнопкой.
RUN_ALL_KEYS = ("leads", "traffic", "finance")

# Каналы, которые Публикатор умеет постить сам (в Telegram-канал клиента).
AUTO_PUBLISH_CHANNELS = ("telegram", "max", "instagram")


def _split(raw: str) -> list[str]:
    """Стоп-слова/CTA из textarea: по строкам или через запятую."""
    parts = [p.strip() for chunk in raw.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


def _back(url: str, ok: str = "", err: str = "") -> RedirectResponse:
    """Редирект с сообщением для пользователя (показывается плашкой сверху)."""
    if ok:
        url += ("&" if "?" in url else "?") + "ok=" + quote(ok)
    elif err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(url, status_code=303)


def page(request: Request, name: str, nav: str, **ctx):
    """Отрисовать страницу с общим контекстом (подсветка меню, счётчик очереди)."""
    ctx.setdefault("nav", nav)
    ctx.setdefault("pending_count",
                   len(db.list_content(statuses=("pending_approval", "needs_human"))))
    return templates.TemplateResponse(request, name, ctx)


def setup_status() -> list[dict]:
    """Чек-лист первичной настройки — что готово, что нет (для главной)."""
    from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    from app.integrations.sheets import KEY_FILE
    google_ok = False
    if KEY_FILE.exists():
        try:
            google_ok = bool(json.loads(KEY_FILE.read_text()).get("client_email"))
        except Exception:
            google_ok = False
    tenants = db.list_tenants()
    any_crm = any(db.get_integration(t["id"], "crm_bitrix24") for t in tenants)
    return [
        {"ok": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID), "title": "Telegram подключён",
         "hint": "Отчёты и уведомления приходят в Telegram", "link": None},
        {"ok": bool(tenants), "title": "Добавлен клиент",
         "hint": "Создайте клиента в форме ниже", "link": None},
        {"ok": any_crm, "title": "У клиента подключена CRM",
         "hint": "Карточка клиента → блок «Интеграции»", "link": None},
        {"ok": google_ok, "title": "Загружен ключ Google",
         "hint": "Нужен для смет, УТП и ТЗ дизайнеру", "link": "/credentials"},
    ]


# ------------------------------------------------------------------- вход ---

PUBLIC_PATHS = ("/login", "/favicon.ico")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Пропускаем без входа только страницу логина. Всё остальное — по cookie."""
    if request.url.path not in PUBLIC_PATHS:
        user_id = auth.verify_session_token(request.cookies.get("session"))
        if user_id is None or db.get_user(user_id) is None:
            return RedirectResponse("/login", status_code=303)
        request.state.user = db.get_user(user_id)
    return await call_next(request)


@app.get("/login")
def login_page(request: Request):
    error = None
    if db.count_users() == 0:
        error = "Пользователей ещё нет. Создайте первого командой: python create_user.py"
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = db.get_user_by_email(email)
    if not user or not auth.verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Неверный email или пароль"})
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", auth.make_session_token(user["id"]),
                    max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# --------------------------------------------------------------- страницы ---

@app.get("/")
def dashboard(request: Request):
    return page(request, "dashboard.html", "dash",
                tenants=db.list_tenants(),
                stats=db.dashboard_stats(),
                activity=db.activity_last_days(7),
                runs=db.list_runs(limit=8),
                setup=setup_status())


@app.get("/tenants/{tenant_id}")
def tenant_page(request: Request, tenant_id: int):
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return _back("/", err="Клиент не найден")
    brand = tenant["brand_profile"]
    return page(request, "tenant.html", "dash",
                t=tenant,
                tone=brand.get("tone", ""),
                stop_words="\n".join(brand.get("stop_words", [])),
                cta_words="\n".join(brand.get("cta_words", [])),
                plan=db.get_plan(tenant_id),
                competitors=db.list_competitors(tenant_id),
                integrations=db.list_integrations(tenant_id),
                agents={k: {"title": v[0], "hint": v[2]} for k, v in AGENTS.items()})


@app.get("/content")
def content_page(request: Request):
    return page(request, "content.html", "content",
                pending=db.list_content(status="pending_approval"),
                needs_human=db.list_content(status="needs_human"),
                resolved=db.list_content(
                    statuses=("published", "approved", "rejected"), limit=20))


@app.get("/runs")
def runs_page(request: Request):
    return page(request, "runs.html", "runs", runs=db.list_runs(limit=50))


@app.get("/runs/{run_id}")
def run_detail(request: Request, run_id: int):
    run = db.get_run(run_id)
    if run is None:
        return _back("/runs", err="Запуск не найден")
    return page(request, "run_detail.html", "runs", r=run)


@app.get("/credentials")
def credentials_page(request: Request):
    from app.integrations.sheets import KEY_FILE
    email, valid = None, False
    if KEY_FILE.exists():
        try:
            data = json.loads(KEY_FILE.read_text())
            email = data.get("client_email")
            valid = bool(email and data.get("private_key"))
        except Exception:
            valid = False
    return page(request, "credentials.html", "creds",
                google_uploaded=valid, google_email=email)


@app.post("/credentials/google")
async def credentials_google(file: UploadFile = File(...)):
    from app.integrations.sheets import KEY_FILE
    content = await file.read()
    try:
        data = json.loads(content)
        assert data.get("client_email") and data.get("private_key")
    except Exception:
        return _back("/credentials", err="Это не похоже на ключ сервисного аккаунта Google")
    KEY_FILE.write_bytes(content)
    return _back("/credentials", ok=f"Ключ загружен: {data['client_email']}")


# ---------------------------------------------------------------- клиенты ---

@app.post("/tenants/add")
def tenant_add(name: str = Form(...), industry: str = Form("")):
    if not name.strip():
        return _back("/", err="Укажите название клиента")
    tenant_id = db.create_tenant(name.strip(), industry.strip(), {
        "tone": "дружелюбно, профессионально, на «вы»",
        "stop_words": [],
        "cta_words": ["запишитесь", "звоните", "пишите"],
    })
    return _back(f"/tenants/{tenant_id}", ok="Клиент создан — заполните профиль бренда")


@app.post("/tenants/{tenant_id}/brand")
def tenant_brand(tenant_id: int, industry: str = Form(""), tone: str = Form(""),
                 stop_words: str = Form(""), cta_words: str = Form("")):
    db.update_tenant(tenant_id, industry.strip(), {
        "tone": tone.strip(),
        "stop_words": _split(stop_words),
        "cta_words": [w.lower() for w in _split(cta_words)],
    })
    return _back(f"/tenants/{tenant_id}", ok="Профиль бренда сохранён")


@app.post("/tenants/{tenant_id}/plan/add")
def plan_add(tenant_id: int, theme: str = Form(...), channel: str = Form(...),
             media: str = Form("")):
    if not theme.strip():
        return _back(f"/tenants/{tenant_id}", err="Укажите тему поста")
    db.add_plan_item(tenant_id, theme.strip(), channel, media.strip())
    return _back(f"/tenants/{tenant_id}", ok="Позиция добавлена в контент-план")


@app.post("/tenants/{tenant_id}/plan/{item_id}/delete")
def plan_delete(tenant_id: int, item_id: int):
    db.delete_plan_item(item_id)
    return _back(f"/tenants/{tenant_id}", ok="Позиция удалена")


@app.post("/tenants/{tenant_id}/competitors/add")
def competitor_add(tenant_id: int, name: str = Form(...), url: str = Form(...)):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not name.strip():
        return _back(f"/tenants/{tenant_id}", err="Укажите название конкурента")
    db.add_competitor(tenant_id, name.strip(), url)
    return _back(f"/tenants/{tenant_id}", ok="Конкурент добавлен")


@app.post("/tenants/{tenant_id}/competitors/{comp_id}/delete")
def competitor_delete(tenant_id: int, comp_id: int):
    db.delete_competitor(comp_id)
    return _back(f"/tenants/{tenant_id}", ok="Конкурент удалён")


# ------------------------------------------------------------- интеграции ---

@app.post("/tenants/{tenant_id}/integrations/bitrix24")
def integration_bitrix24(tenant_id: int, webhook_url: str = Form(...)):
    url = webhook_url.strip()
    if not url.startswith("http"):
        return _back(f"/tenants/{tenant_id}", err="Вставьте полный URL вебхука Битрикс24")
    db.save_integration(tenant_id, "crm_bitrix24", {"webhook_url": url})
    msg = integrations_service.test_crm(tenant_id, "crm_bitrix24")
    return _back(f"/tenants/{tenant_id}", ok=f"CRM сохранена: {msg}")


@app.post("/tenants/{tenant_id}/integrations/calltouch")
def integration_calltouch(tenant_id: int, token: str = Form(...), site_id: str = Form(...)):
    if not (token.strip() and site_id.strip()):
        return _back(f"/tenants/{tenant_id}", err="Нужны и токен, и ID сайта Calltouch")
    db.save_integration(tenant_id, "calltouch",
                        {"token": token.strip(), "site_id": site_id.strip()})
    msg = integrations_service.test_calltouch(tenant_id)
    return _back(f"/tenants/{tenant_id}", ok=f"Calltouch сохранён: {msg}")


@app.post("/tenants/{tenant_id}/integrations/estimates")
def integration_estimates(tenant_id: int, sheet_id: str = Form(...),
                          worksheet: str = Form("")):
    sid = sheet_id.strip()
    if "/d/" in sid:                       # вставили полную ссылку — достаём ID
        sid = sid.split("/d/", 1)[1].split("/", 1)[0]
    if not sid:
        return _back(f"/tenants/{tenant_id}", err="Вставьте ссылку на Google Таблицу")
    db.save_integration(tenant_id, "estimates_sheet",
                        {"sheet_id": sid, "worksheet": worksheet.strip()})
    return _back(f"/tenants/{tenant_id}", ok="Таблица смет подключена")


@app.post("/tenants/{tenant_id}/integrations/utp")
def integration_utp(tenant_id: int, sheet_id: str = Form(...)):
    sid = sheet_id.strip()
    if "/d/" in sid:
        sid = sid.split("/d/", 1)[1].split("/", 1)[0]
    if not sid:
        return _back(f"/tenants/{tenant_id}", err="Вставьте ссылку на Google Таблицу")
    db.save_integration(tenant_id, "utp_sheet", {"sheet_id": sid})
    msg = integrations_service.test_sheet(tenant_id, "utp_sheet")
    return _back(f"/tenants/{tenant_id}", ok=f"Таблица УТП подключена — {msg}")


@app.post("/tenants/{tenant_id}/integrations/{kind}/test")
def integration_test(tenant_id: int, kind: str):
    if kind == "calltouch":
        msg = integrations_service.test_calltouch(tenant_id)
    elif kind in ("estimates_sheet", "utp_sheet"):
        msg = integrations_service.test_sheet(tenant_id, kind)
    else:
        msg = integrations_service.test_crm(tenant_id, kind)
    return _back(f"/tenants/{tenant_id}", ok=msg)


@app.post("/tenants/{tenant_id}/integrations/{integration_id}/delete")
def integration_delete(tenant_id: int, integration_id: int):
    db.delete_integration(integration_id)
    return _back(f"/tenants/{tenant_id}", ok="Интеграция отключена")


@app.post("/tenants/{tenant_id}/crm-sync")
def crm_sync(tenant_id: int, background: BackgroundTasks):
    background.add_task(integrations_service.sync_crm, tenant_id)
    return _back("/runs", ok="Проверяю данные CRM — результат появится здесь через минуту")


LEAD_UNIFIED = ["NEW", "IN_PROGRESS", "QUALIFIED", "REJECTED"]
DEAL_UNIFIED = ["NEW", "QUALIFIED", "PROPOSAL", "WON", "LOST"]
UNIFIED_RU = {
    "NEW": "NEW — новый, ещё не в работе",
    "IN_PROGRESS": "IN_PROGRESS — взят в работу",
    "QUALIFIED": "QUALIFIED — квалифицирован",
    "REJECTED": "REJECTED — отказ",
    "PROPOSAL": "PROPOSAL — предложение отправлено",
    "WON": "WON — сделка выиграна (продажа)",
    "LOST": "LOST — сделка проиграна",
}
templates.env.globals["uru"] = lambda u: UNIFIED_RU.get(u, u)


@app.get("/tenants/{tenant_id}/stages")
def stages_page(request: Request, tenant_id: int):
    adapter = integrations_service.build_crm_adapter(tenant_id)
    stages, error = [], None
    if adapter is None:
        error = "Сначала подключите CRM в карточке клиента."
    else:
        try:
            stages = adapter.get_stages()
        except Exception as e:
            error = f"Не удалось получить стадии из CRM: {e}"
    return page(request, "stages.html", "dash",
                tenant_id=tenant_id,
                lead_stages=[s for s in stages if s["entity"] == "lead"],
                deal_stages=[s for s in stages if s["entity"] == "deal"],
                current=db.get_stage_mappings(tenant_id),
                lead_unified=LEAD_UNIFIED, deal_unified=DEAL_UNIFIED,
                error=error)


@app.post("/tenants/{tenant_id}/stages")
async def stages_save(request: Request, tenant_id: int):
    form = await request.form()
    saved = 0
    for key, val in form.items():
        if "::" in key and val:            # ключи вида "lead::NEW" / "deal::WON"
            entity, raw_code = key.split("::", 1)
            if entity in ("lead", "deal"):
                db.set_stage_mapping(tenant_id, entity, raw_code, val)
                saved += 1
    return _back(f"/tenants/{tenant_id}/stages", ok=f"Сохранено стадий: {saved}")


# --------------------------------------------------------------- действия ---

@app.post("/tenants/{tenant_id}/run/{agent_key}")
def run_agent(tenant_id: int, agent_key: str, background: BackgroundTasks):
    if agent_key not in AGENTS or not db.get_tenant(tenant_id):
        return _back("/runs", err="Задача не найдена")
    title, fn, _ = AGENTS[agent_key]
    background.add_task(fn, tenant_id)
    return _back("/runs", ok=f"«{title}» запущен(а) — результат появится здесь через 1–3 минуты")


@app.post("/tenants/{tenant_id}/run-all")
def run_all(tenant_id: int, background: BackgroundTasks):
    """Одной кнопкой — все проверки по клиенту (аналитика, без генерации контента)."""
    if not db.get_tenant(tenant_id):
        return _back("/", err="Клиент не найден")
    for key in RUN_ALL_KEYS:
        background.add_task(AGENTS[key][1], tenant_id)
    return _back("/runs", ok=f"Запущено проверок: {len(RUN_ALL_KEYS)} — "
                            f"результаты появятся здесь через несколько минут")


@app.post("/content/{content_id}/approve")
def approve(content_id: int):
    item = db.get_content(content_id)
    if not item or item["status"] not in ("pending_approval", "needs_human"):
        return _back("/content", err="Этот черновик уже обработан")
    if item["channel"] in AUTO_PUBLISH_CHANNELS:
        status_msg, post_id = tg.publish_to_channel(item["body"])
        db.set_content_status(content_id, "published" if post_id else "approved",
                              post_id, note=status_msg)
        if post_id is None:
            # публикация НЕ прошла — кричим владельцу в личку, а не молчим
            tg.notify(f"⚠ Пост #{content_id} ({item['theme']}) согласован, "
                      f"но НЕ опубликован: {status_msg}")
            return _back("/content", err=f"Согласовано, но не опубликовано: {status_msg}")
        return _back("/content", ok="Опубликовано в канал")
    db.set_content_status(content_id, "approved",
                          note="утверждено; отправка через SMS/WA-агрегатора")
    return _back("/content", ok="Утверждено — готово к отправке")


@app.post("/content/{content_id}/reject")
def reject(content_id: int):
    item = db.get_content(content_id)
    if not item or item["status"] not in ("pending_approval", "needs_human"):
        return _back("/content", err="Этот черновик уже обработан")
    db.set_content_status(content_id, "rejected")
    return _back("/content", ok="Черновик отклонён")
