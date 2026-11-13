# -*- coding: utf-8 -*-
"""案件导入与仓储: 申请号归一化、校验位拦截、任务联动。"""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass

from common.checksum import normalize
from patent_monitor.db import now


@dataclass
class ImportRow:
    raw_app_no: str
    customer: str = ""
    case_alias: str = ""


@dataclass
class ImportReport:
    total: int = 0
    inserted: int = 0
    duplicated: int = 0
    invalid: int = 0
    invalid_rows: list[dict] | None = None

    def __post_init__(self):
        if self.invalid_rows is None:
            self.invalid_rows = []


def import_rows(conn: sqlite3.Connection, rows: list[ImportRow]) -> ImportReport:
    """导入案件行。校验不过的案件也入库(valid=0)并标出原因,但不建采集任务。"""
    rep = ImportReport()
    with conn:
        for row in rows:
            rep.total += 1
            n = normalize(row.raw_app_no)
            ts = now()
            if not n.ok:
                rep.invalid += 1
                rep.invalid_rows.append(
                    {"raw": row.raw_app_no, "reason": n.reason,
                     "suggest": n.normalized or ""})
                # 无效案件用独立命名空间的键, 避免其推算值与某有效案件
                # 申请号撞 UNIQUE 而被静默丢弃
                key = f"INVALID::{row.raw_app_no.strip()}"
                conn.execute(
                    "INSERT OR IGNORE INTO cases(app_no,raw_input,customer,"
                    "case_alias,check_valid,check_reason,valid,created_at) "
                    "VALUES(?,?,?,?,0,?,0,?)",
                    (key, row.raw_app_no, row.customer, row.case_alias,
                     n.reason, ts))
                continue
            conn.execute(
                "INSERT INTO cases(app_no,raw_input,customer,case_alias,"
                "check_valid,check_reason,valid,created_at) "
                "VALUES(?,?,?,?,1,'',1,?) ON CONFLICT(app_no) DO UPDATE SET "
                "customer=excluded.customer, case_alias=excluded.case_alias",
                (n.normalized, row.raw_app_no, row.customer,
                 row.case_alias, ts))
            case_row = conn.execute(
                "SELECT id FROM cases WHERE app_no=?", (n.normalized,)).fetchone()
            task_row = conn.execute(
                "SELECT 1 FROM tasks WHERE case_id=?", (case_row[0],)).fetchone()
            if task_row is None:
                conn.execute(
                    "INSERT INTO tasks(case_id,status,next_run_at) "
                    "VALUES(?, 'pending', NULL)",
                    (case_row[0],))
                rep.inserted += 1
            else:
                rep.duplicated += 1
    return rep


def parse_file(filename: str, content: bytes) -> list[ImportRow]:
    """解析 CSV / XLSX 上传文件。表头识别 申请号/客户/备注 列。"""
    name = filename.lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        text = content.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        data = list(reader)
    elif name.endswith(".xlsx") or name.endswith(".xls"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        data = [[("" if c is None else str(c)) for c in r]
                for r in ws.iter_rows(values_only=True)]
    else:
        raise ValueError("仅支持 .csv/.txt/.xlsx 文件")

    if not data:
        return []
    header = [h.strip() for h in data[0]]
    col = {"app": 0, "customer": 1, "alias": 2}
    for i, h in enumerate(header):
        if "申请号" in h or "专利号" in h:
            col["app"] = i
        elif "客户" in h or "申请人" in h or "公司" in h:
            col["customer"] = i
        elif "备注" in h or "名称" in h or "别名" in h:
            col["alias"] = i
    has_header = any("申请号" in h or "专利号" in h for h in header)
    body = data[1:] if has_header else data
    out = []
    for r in body:
        if not r or not str(r[col["app"]]).strip():
            continue
        out.append(ImportRow(
            raw_app_no=str(r[col["app"]]).strip(),
            customer=(r[col["customer"]] if len(r) > col["customer"] else "") or "",
            case_alias=(r[col["alias"]] if len(r) > col["alias"] else "") or ""))
    return out


# ---------------------------------------------------------------- 仓储查询
def list_cases(conn, customer=None, status=None, due_level=None,
               q=None, limit=500, offset=0):
    sql = ("SELECT c.*, ls.legal_status, ls.captured_at AS last_poll, "
           "rm.level AS due_level, rm.due_date, rm.fee_year "
           "FROM cases c "
           "LEFT JOIN (SELECT s1.* FROM snapshots s1 JOIN "
           "(SELECT case_id,MAX(captured_at) m FROM snapshots GROUP BY case_id) m1 "
           "ON s1.case_id=m1.case_id AND s1.captured_at=m1.m) ls ON ls.case_id=c.id "
           "LEFT JOIN (SELECT * FROM reminders WHERE paid=0) rm ON rm.case_id=c.id "
           "WHERE 1=1 ")
    args: list = []
    if customer:
        sql += "AND c.customer=? "; args.append(customer)
    if status:
        sql += "AND ls.legal_status=? "; args.append(status)
    if due_level:
        sql += "AND rm.level=? "; args.append(due_level)
    if q:
        sql += "AND (c.app_no LIKE ? OR c.case_alias LIKE ?) "
        args += [f"%{q}%", f"%{q}%"]
    sql += "ORDER BY c.id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return conn.execute(sql, args).fetchall()


def count_cases(conn, **filters) -> int:
    rows = list_cases(conn, limit=10_000_000, offset=0, **filters)
    return len(rows)


def distinct_customers(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT customer FROM cases WHERE customer<>'' ORDER BY 1")]


def distinct_statuses(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT legal_status FROM snapshots "
        "WHERE legal_status IS NOT NULL ORDER BY 1")]
