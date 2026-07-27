"""
Слой базы данных (SQLite, стандартная библиотека).

Это версия v0.1 модели данных из архитектурной спецификации, ужатая до четырёх
таблиц. Когда проектом займётся команда, этот модуль заменяется на
SQLAlchemy + PostgreSQL — весь остальной код обращается только к функциям
отсюда, поэтому замена пройдёт безболезненно (паттерн Repository).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    industry      TEXT,
    brand_profile TEXT NOT NULL DEFAULT '{}'      -- JSON: tone, stop_words, cta_words
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'owner',
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS content_plan (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    theme     TEXT NOT NULL,
    channel   TEXT NOT NULL,
    media     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS integrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
    kind        TEXT NOT NULL,          -- crm_bitrix24 | crm_amocrm | calltouch
    credentials TEXT NOT NULL DEFAULT '{}',  -- JSON: webhook_url / token / node_id
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | active | error
    last_note   TEXT NOT NULL DEFAULT '',
    last_sync   TEXT,
    UNIQUE (tenant_id, kind)
);

CREATE TABLE IF NOT EXISTS stage_mappings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    entity    TEXT NOT NULL,          -- lead | deal
    raw_code  TEXT NOT NULL,          -- код стадии в CRM клиента
    unified   TEXT NOT NULL,          -- наш статус: NEW|IN_PROGRESS|QUALIFIED|...
    UNIQUE (tenant_id, entity, raw_code)
);

CREATE TABLE IF NOT EXISTS competitors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name      TEXT NOT NULL,
    url       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competitor_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    content_text  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
    graph_name  TEXT NOT NULL,                    -- content_weekly | traffic_report | lead_control
    status      TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
    output      TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS generated_content (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id),
    run_id        INTEGER REFERENCES agent_runs(id),
    channel       TEXT NOT NULL,
    theme         TEXT NOT NULL,
    body          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending_approval',
                  -- pending_approval | approved | rejected | published | needs_human
    problems      TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 1,
    external_post_id TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row     # строки как словари: row["name"]
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: панель и планировщик - два процесса, пишущих в одну базу;
    # без этого возможны ошибки "database is locked"
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- tenants ---

def create_tenant(name: str, industry: str, brand_profile: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, industry, brand_profile) VALUES (?,?,?)",
            (name, industry, json.dumps(brand_profile, ensure_ascii=False)))
        return cur.lastrowid


def get_tenant(tenant_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    if row is None:
        return None
    tenant = dict(row)
    tenant["brand_profile"] = json.loads(tenant["brand_profile"])
    return tenant


def list_tenants() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tenants ORDER BY id")]


def update_tenant(tenant_id: int, industry: str, brand_profile: dict) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE tenants SET industry=?, brand_profile=? WHERE id=?",
            (industry, json.dumps(brand_profile, ensure_ascii=False), tenant_id))


# ------------------------------------------------------------------ users ---

def create_user(email: str, password_hash: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?,?)",
            (email.lower().strip(), password_hash))
        return cur.lastrowid


def get_user_by_email(email: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?",
                           (email.lower().strip(),)).fetchone()
    return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def count_users() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


# ----------------------------------------------------------- content plan ---

def add_plan_item(tenant_id: int, theme: str, channel: str, media: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO content_plan (tenant_id, theme, channel, media) VALUES (?,?,?,?)",
            (tenant_id, theme, channel, media))
        return cur.lastrowid


def delete_plan_item(item_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM content_plan WHERE id=?", (item_id,))


def get_plan(tenant_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM content_plan WHERE tenant_id=? ORDER BY id", (tenant_id,))]


# ------------------------------------------------------------ integrations ---

def save_integration(tenant_id: int, kind: str, credentials: dict) -> int:
    """Создать или обновить интеграцию клиента (по паре tenant+kind)."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO integrations (tenant_id, kind, credentials) VALUES (?,?,?) "
            "ON CONFLICT(tenant_id, kind) DO UPDATE SET credentials=excluded.credentials, "
            "status='pending' RETURNING id",
            (tenant_id, kind, json.dumps(credentials, ensure_ascii=False)))
        return cur.fetchone()["id"]


def get_integration(tenant_id: int, kind: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM integrations WHERE tenant_id=? AND kind=?",
                           (tenant_id, kind)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["credentials"] = json.loads(d["credentials"])
    return d


def list_integrations(tenant_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM integrations WHERE tenant_id=? ORDER BY kind",
                            (tenant_id,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["credentials"] = json.loads(d["credentials"])
        out.append(d)
    return out


def set_integration_status(integration_id: int, status: str, note: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE integrations SET status=?, last_note=?, "
            "last_sync=datetime('now','localtime') WHERE id=?",
            (status, note, integration_id))


def delete_integration(integration_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM integrations WHERE id=?", (integration_id,))


# ----------------------------------------------------------- stage mapping ---

def set_stage_mapping(tenant_id: int, entity: str, raw_code: str, unified: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO stage_mappings (tenant_id, entity, raw_code, unified) "
            "VALUES (?,?,?,?) ON CONFLICT(tenant_id, entity, raw_code) "
            "DO UPDATE SET unified=excluded.unified",
            (tenant_id, entity, raw_code, unified))


def get_stage_mappings(tenant_id: int) -> dict[str, dict[str, str]]:
    """Вернёт {'lead': {raw_code: unified}, 'deal': {...}}."""
    out: dict[str, dict[str, str]] = {"lead": {}, "deal": {}}
    with connect() as conn:
        for row in conn.execute("SELECT entity, raw_code, unified FROM stage_mappings "
                                "WHERE tenant_id=?", (tenant_id,)):
            out.setdefault(row["entity"], {})[row["raw_code"]] = row["unified"]
    return out


# ------------------------------------------------------------ competitors ---

def add_competitor(tenant_id: int, name: str, url: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO competitors (tenant_id, name, url) VALUES (?,?,?)",
            (tenant_id, name, url))
        return cur.lastrowid


def list_competitors(tenant_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM competitors WHERE tenant_id=? ORDER BY id", (tenant_id,))]


def delete_competitor(comp_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM competitor_snapshots WHERE competitor_id=?", (comp_id,))
        conn.execute("DELETE FROM competitors WHERE id=?", (comp_id,))


def last_snapshot(comp_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM competitor_snapshots WHERE competitor_id=? "
            "ORDER BY id DESC LIMIT 1", (comp_id,)).fetchone()
    return dict(row) if row else None


def save_snapshot(comp_id: int, text: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO competitor_snapshots (competitor_id, content_text) VALUES (?,?)",
            (comp_id, text))


# ------------------------------------------------------------- agent runs ---

def start_run(tenant_id: int, graph_name: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO agent_runs (tenant_id, graph_name) VALUES (?,?)",
            (tenant_id, graph_name))
        return cur.lastrowid


def finish_run(run_id: int, status: str, output: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status=?, output=?, "
            "finished_at=datetime('now','localtime') WHERE id=?",
            (status, output, run_id))


def list_runs(limit: int = 20) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT r.*, t.name AS tenant_name FROM agent_runs r "
            "JOIN tenants t ON t.id = r.tenant_id "
            "ORDER BY r.id DESC LIMIT ?", (limit,))]


# ------------------------------------------------------ generated content ---

def save_content(tenant_id: int, run_id: int, channel: str, theme: str,
                 body: str, status: str, problems: list[str], attempts: int) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO generated_content "
            "(tenant_id, run_id, channel, theme, body, status, problems, attempts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tenant_id, run_id, channel, theme, body, status,
             "; ".join(problems), attempts))
        return cur.lastrowid


def get_content(content_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM generated_content WHERE id=?",
                           (content_id,)).fetchone()
    return dict(row) if row else None


def list_content(status: str | None = None, limit: int = 50) -> list[dict]:
    q = ("SELECT c.*, t.name AS tenant_name FROM generated_content c "
         "JOIN tenants t ON t.id = c.tenant_id ")
    params: tuple = ()
    if status:
        q += "WHERE c.status=? "
        params = (status,)
    q += "ORDER BY c.id DESC LIMIT ?"
    with connect() as conn:
        return [dict(r) for r in conn.execute(q, params + (limit,))]


def set_content_status(content_id: int, status: str,
                       external_post_id: str | None = None,
                       note: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE generated_content SET status=?, "
            "external_post_id=COALESCE(?, external_post_id), "
            "problems=COALESCE(?, problems) WHERE id=?",
            (status, external_post_id, note, content_id))


def counts_by_status() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM generated_content GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
