# -*- coding: utf-8 -*-
"""SQLite 持久层: 案件、快照、差异、任务、提醒、设置、日志。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

from patent_monitor.config import DEFAULT_SETTINGS, ROOT, Settings

DB_PATH = os.path.join(ROOT, "data", "patent_monitor.db")
_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_no TEXT UNIQUE NOT NULL,          -- 归一化后的申请号
    raw_input TEXT,                       -- 原始导入文本
    customer TEXT,                        -- 客户/申请人
    case_alias TEXT,                      -- 案件备注名
    check_valid INTEGER NOT NULL DEFAULT 1,
    check_reason TEXT,
    valid INTEGER NOT NULL DEFAULT 1,     -- 0=校验不过, 不参与采集
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    legal_status TEXT,
    status_date TEXT,
    assignee TEXT,
    grant_date TEXT,
    title TEXT,
    patent_type TEXT,
    fee_json TEXT NOT NULL,               -- 年费记录完整 JSON
    UNIQUE(case_id, captured_at),
    FOREIGN KEY(case_id) REFERENCES cases(id)
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    field TEXT NOT NULL,                  -- legal_status / assignee / fee...
    old_value TEXT,
    new_value TEXT,
    detail TEXT,
    FOREIGN KEY(case_id) REFERENCES cases(id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    -- pending / in_progress / done / failed / banned_wait / invalid
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_run_at TEXT,
    next_run_at TEXT,                     -- 封禁恢复后从该时间起
    FOREIGN KEY(case_id) REFERENCES cases(id)
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    fee_year INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    grace_end TEXT NOT NULL,
    amount INTEGER,
    level TEXT NOT NULL,                  -- normal / due_soon / overdue / grace
    paid INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, fee_year)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total INTEGER DEFAULT 0,
    success INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0,
    cursor_pos INTEGER NOT NULL DEFAULT 0,  -- 断点续采: 下一个待处理下标
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_changes_case ON changes(case_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_reminders_level ON reminders(level);
"""


def now() -> str:
    # 毫秒精度, 避免同一案件同秒多次采集撞快照唯一约束
    return datetime.now().isoformat(timespec="milliseconds")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _LOCK:
        conn.executescript(SCHEMA)
        seed_settings(conn)


# ---------------------------------------------------------------- settings
def seed_settings(conn: sqlite3.Connection) -> None:
    for k, v in DEFAULT_SETTINGS.to_dict().items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            (k, json.dumps(v)))
    conn.commit()


def load_settings(conn: sqlite3.Connection) -> Settings:
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    raw = {r["key"]: json.loads(r["value"]) for r in rows}
    return Settings.from_dict(raw)


def save_settings(conn: sqlite3.Connection, data: dict) -> Settings:
    s = Settings.from_dict({**load_settings(conn).to_dict(), **data})
    with _LOCK:
        for k, v in s.to_dict().items():
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)))
        conn.commit()
    return s
