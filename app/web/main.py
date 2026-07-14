"""
Веб-панель платформы (v0.2).

Страницы:
  /                — обзор: клиенты, счётчики, последние запуски
  /tenants/{id}    — карточка клиента: профиль бренда, контент-план, запуск агентов
  /content         — очередь контента с кнопками согласования
  /runs            — журнал запусков

Запуск:  python run_web.py  ->  http://127.0.0.1:8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db, tg
from app.agents import content as content_agent
from app.agents import leads as leads_agent
from app.agents import traffic as traffic_agent

app = FastAPI(title="MAS Platform", version="0.2.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

db.init_db()

AGENTS = {
    "content": ("Копирайтер: контент недели", content_agent.run),
    "traffic": ("Аналитик: отчёт по трафику", traffic_agent.run),
    "leads": ("Контролёр: сверка лидов", leads_agent.run),
}


def _split(raw: str) -> list[str]:
    """Стоп-слова/CTA из textarea: по строкам или через запятую."""
    parts = [p.strip() for chunk in raw.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


# ------------------------------------------------------------------ pages ---

@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "tenants": db.list_tenants(),
        "counts": db.counts_by_status(),
        "runs": db.list_runs(limit=8),
    })


@app.get("/tenants/{tenant_id}")
def tenant_page(request: Request, tenant_id: int):
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse("/", status_code=303)
    brand = tenant["brand_profile"]
    return templates.TemplateResponse(request, "tenant.html", {
        "t": tenant,
        "tone": brand.get("tone", ""),
        "stop_words": "\n".join(brand.get("stop_words", [])),
        "cta_words": "\n".join(brand.get("cta_words", [])),
        "plan": db.get_plan(tenant_id),
        "agents": {key: title for key, (title, _) in AGENTS.items()},
    })


@app.get("/content")
def content_page(request: Request):
    return templates.TemplateResponse(request, "content.html", {
        "pending": db.list_content(status="pending_approval"),
        "needs_human": db.list_content(status="needs_human"),
        "resolved": [c for c in db.list_content()
                     if c["status"] in ("published", "approved", "rejected")][:20],
    })


@app.get("/runs")
def runs_page(request: Request):
    return templates.TemplateResponse(request, "runs.html",
                                      {"runs": db.list_runs(limit=50)})


# ---------------------------------------------------------------- tenants ---

@app.post("/tenants/add")
def tenant_add(name: str = Form(...), industry: str = Form("")):
    tenant_id = db.create_tenant(name.strip(), industry.strip(), {
        "tone": "дружелюбно, профессионально, на «вы»",
        "stop_words": [],
        "cta_words": ["запишитесь", "звоните", "пишите"],
    })
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@app.post("/tenants/{tenant_id}/brand")
def tenant_brand(tenant_id: int, industry: str = Form(""), tone: str = Form(""),
                 stop_words: str = Form(""), cta_words: str = Form("")):
    db.update_tenant(tenant_id, industry.strip(), {
        "tone": tone.strip(),
        "stop_words": _split(stop_words),
        "cta_words": [w.lower() for w in _split(cta_words)],
    })
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@app.post("/tenants/{tenant_id}/plan/add")
def plan_add(tenant_id: int, theme: str = Form(...), channel: str = Form(...),
             media: str = Form("")):
    if theme.strip():
        db.add_plan_item(tenant_id, theme.strip(), channel, media.strip())
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@app.post("/tenants/{tenant_id}/plan/{item_id}/delete")
def plan_delete(tenant_id: int, item_id: int):
    db.delete_plan_item(item_id)
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


# ---------------------------------------------------------------- actions ---

@app.post("/tenants/{tenant_id}/run/{agent_key}")
def run_agent(tenant_id: int, agent_key: str, background: BackgroundTasks):
    if agent_key in AGENTS and db.get_tenant(tenant_id):
        _, fn = AGENTS[agent_key]
        background.add_task(fn, tenant_id)
    return RedirectResponse("/runs", status_code=303)


@app.post("/content/{content_id}/approve")
def approve(content_id: int):
    item = db.get_content(content_id)
    if item and item["status"] in ("pending_approval", "needs_human"):
        status_msg, post_id = tg.publish_to_channel(item["body"])
        db.set_content_status(content_id,
                              "published" if post_id else "approved", post_id)
    return RedirectResponse("/content", status_code=303)


@app.post("/content/{content_id}/reject")
def reject(content_id: int):
    item = db.get_content(content_id)
    if item and item["status"] in ("pending_approval", "needs_human"):
        db.set_content_status(content_id, "rejected")
    return RedirectResponse("/content", status_code=303)
