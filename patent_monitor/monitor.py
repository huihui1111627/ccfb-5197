# -*- coding: utf-8 -*-
"""快照落库、状态差异比对、年费期限与提醒计算。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from patent_monitor.db import now
from patent_monitor.collector import FetchResult


# ---------------------------------------------------------------- 差异
def diff_snapshots(prev: dict | None, curr: dict) -> list[dict]:
    """对比两次查询结果, 返回字段级差异。prev=None 表示首次建档。"""
    changes: list[dict] = []
    if prev is None:
        changes.append({"field": "init", "old_value": None,
                        "new_value": "首次采集建档", "detail": ""})
        return changes

    for field in ("legalStatus", "assignee", "grantDate", "title",
                  "patentType", "statusDate"):
        if prev.get(field) != curr.get(field):
            label = {"legalStatus": "法律状态", "assignee": "专利权人",
                     "grantDate": "授权日", "title": "名称",
                     "patentType": "专利类型", "statusDate": "状态日期"}[field]
            changes.append({"field": field,
                            "old_value": str(prev.get(field)),
                            "new_value": str(curr.get(field)),
                            "detail": f"{label}变化"})

    prev_fees = {f["year"]: f for f in prev.get("feeRecords", [])}
    curr_fees = {f["year"]: f for f in curr.get("feeRecords", [])}
    for year in sorted(set(prev_fees) | set(curr_fees)):
        pf, cf = prev_fees.get(year), curr_fees.get(year)
        if pf is None and cf is not None:
            changes.append({"field": f"fee_new_y{year}",
                            "old_value": None,
                            "new_value": json.dumps(cf, ensure_ascii=False),
                            "detail": f"新增第{year}年年费记录"})
        elif cf is None:
            changes.append({"field": f"fee_gone_y{year}",
                            "old_value": json.dumps(pf, ensure_ascii=False),
                            "new_value": None,
                            "detail": f"第{year}年年费记录消失"})
        elif pf.get("paid") != cf.get("paid"):
            changes.append({
                "field": f"fee_paid_y{year}",
                "old_value": "已缴" if pf.get("paid") else "未缴",
                "new_value": "已缴" if cf.get("paid") else "未缴",
                "detail": f"第{year}年年费缴纳状态变化"
                          + (f"(缴费日 {cf.get('paidDate')})"
                             if cf.get("paid") else "")})
        elif pf.get("amount") != cf.get("amount"):
            changes.append({"field": f"fee_amount_y{year}",
                            "old_value": str(pf.get("amount")),
                            "new_value": str(cf.get("amount")),
                            "detail": f"第{year}年年费金额变化"})
    return changes


def latest_snapshot(conn: sqlite3.Connection, case_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE case_id=? ORDER BY captured_at DESC "
        "LIMIT 1", (case_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    fees = json.loads(d.pop("fee_json"))
    d.pop("id", None)
    d.pop("case_id", None)
    # DB snake_case 列映射回接口 camelCase 字段, 供 diff_snapshots 对齐
    return {
        "legalStatus": d.get("legal_status"),
        "statusDate": d.get("status_date"),
        "assignee": d.get("assignee"),
        "grantDate": d.get("grant_date"),
        "title": d.get("title"),
        "patentType": d.get("patent_type"),
        "feeRecords": fees,
    }


def save_snapshot(conn: sqlite3.Connection, case_id: int, data: dict,
                  captured_at: str) -> list[dict]:
    """落快照并写差异, 返回本次差异列表。"""
    prev = latest_snapshot(conn, case_id)
    changes = diff_snapshots(prev, data)
    # 同一案件可能在同一时间戳内被采集两次(虚拟时钟/高并发), 撞唯一约束时
    # 追加可排序的序号后缀, 保证快照时间戳唯一
    stamp = captured_at
    seq = 1
    while conn.execute(
            "SELECT 1 FROM snapshots WHERE case_id=? AND captured_at=?",
            (case_id, stamp)).fetchone():
        stamp = f"{captured_at}#{seq:03d}"
        seq += 1
    with conn:
        conn.execute(
            "INSERT INTO snapshots(case_id,captured_at,legal_status,"
            "status_date,assignee,grant_date,title,patent_type,fee_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (case_id, stamp, data.get("legalStatus"),
             data.get("statusDate"), data.get("assignee"),
             data.get("grantDate"), data.get("title"),
             data.get("patentType"),
             json.dumps(data.get("feeRecords", []), ensure_ascii=False)))
        for ch in changes:
            conn.execute(
                "INSERT INTO changes(case_id,changed_at,field,old_value,"
                "new_value,detail) VALUES(?,?,?,?,?,?)",
                (case_id, captured_at, ch["field"], ch["old_value"],
                 ch["new_value"], ch["detail"]))
    return changes


# ---------------------------------------------------------------- 年费期限
def fee_deadline(app_date_iso: str, year: int,
                 advance_days: int = 0) -> date:
    """第 year 年年费应缴日 = 申请日在申请年+year 的对应日, 可提前 advance_days。

    注: 实务中申请日在月末(如 2/29)对应日依法顺延, 这里用 date 构造兜底。
    """
    app_d = datetime.strptime(app_date_iso, "%Y-%m-%d").date()
    y = app_d.year + year
    try:
        due = date(y, app_d.month, app_d.day)
    except ValueError:
        # 2/29 -> 2/28
        due = date(y, app_d.month, 28)
    return due - timedelta(days=advance_days)


def refresh_reminders(conn: sqlite3.Connection, case_id: int, data: dict,
                      remind_days: int, grace_days: int,
                      advance_days: int, today_: date | None = None) -> int:
    """根据最新快照重建该案件的未缴年费提醒, 返回提醒条数。"""
    today_ = today_ or date.today()
    app_date = data["appDate"]
    rows = []
    for fee in data.get("feeRecords", []):
        if fee.get("paid"):
            continue
        # 优先采用接口返回的应缴日, 缺失时才按申请日对应日推算
        if fee.get("dueDate"):
            due = datetime.strptime(fee["dueDate"], "%Y-%m-%d").date()
        else:
            due = fee_deadline(app_date, fee["year"], advance_days)
        grace_end = due + timedelta(days=grace_days)
        days_left = (due - today_).days
        if days_left > remind_days:
            continue               # 还早, 不进提醒表
        if today_ <= due:
            level = "due_soon"     # 提醒窗口内但尚未到应缴日
        elif today_ <= grace_end:
            level = "grace"        # 过应缴日, 仍在宽限期(滞纳期)内
        else:
            level = "overdue"      # 超过宽限期仍未缴
        rows.append((case_id, fee["year"], due.isoformat(),
                     grace_end.isoformat(), fee.get("amount"), level,
                     now()))
    with conn:
        conn.execute("DELETE FROM reminders WHERE case_id=?", (case_id,))
        conn.executemany(
            "INSERT INTO reminders(case_id,fee_year,due_date,grace_end,"
            "amount,level,paid,updated_at) VALUES(?,?,?,?,?,?,0,?)", rows)
    return len(rows)


# ---------------------------------------------------------------- 组合
def ingest_result(conn: sqlite3.Connection, case_id: int,
                  result: FetchResult, settings,
                  today_: date | None = None) -> list[dict]:
    """一次采集结果的完整落库: 快照+差异+提醒。"""
    changes = save_snapshot(conn, case_id, result.data, result.fetched_at)
    refresh_reminders(conn, case_id, result.data,
                      settings.remind_days, settings.grace_days,
                      settings.renew_advance_days, today_)
    return changes
