# -*- coding: utf-8 -*-
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.checksum import compute_check_digit  # noqa: E402
from patent_monitor.config import Settings  # noqa: E402
from patent_monitor.db import connect, init_db  # noqa: E402
from patent_monitor.importer import ImportRow, import_rows  # noqa: E402
from patent_monitor.collector import FetchResult  # noqa: E402
from patent_monitor.monitor import (fee_deadline,  # noqa: E402
                                    ingest_result, refresh_reminders)


@pytest.fixture()
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_db(c)
    yield c
    c.close()


def _valid_no(year=2020, serial=1000001):
    base = f"{year}1{serial:07d}"
    return base + compute_check_digit(base)


def test_import_valid_and_invalid(conn):
    good = _valid_no()
    rep = import_rows(conn, [
        ImportRow(good, "客户甲", "案件A"),
        ImportRow(good[:-1] + ("0" if good[-1] != "0" else "1"), "客户乙"),
        ImportRow("GARBAGE", "客户丙"),
    ])
    assert rep.total == 3 and rep.inserted == 1 and rep.invalid == 2
    valid = conn.execute("SELECT * FROM cases WHERE valid=1").fetchall()
    invalid = conn.execute("SELECT * FROM cases WHERE valid=0").fetchall()
    assert len(valid) == 1 and len(invalid) == 2
    # 有效案件自动建 pending 任务, 无效案件不建
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_import_dedup(conn):
    good = _valid_no()
    r1 = import_rows(conn, [ImportRow(good, "甲")])
    r2 = import_rows(conn, [ImportRow(good, "甲")])
    assert r1.inserted == 1 and r2.inserted == 0 and r2.duplicated == 1


def test_diff_detects_status_assignee_and_fee(conn):
    app_no = _valid_no()
    import_rows(conn, [ImportRow(app_no, "甲")])
    case_id = conn.execute("SELECT id FROM cases").fetchone()[0]
    base_patent = {
        "appNo": app_no, "appDate": "2020-06-15", "title": "t",
        "patentType": "发明", "grantDate": "2022-01-01",
        "legalStatus": "有效", "statusDate": "2020-06-15",
        "assignee": "甲", "feeRecords": [
            {"year": 1, "dueDate": "2021-06-15", "amount": 900,
             "paid": True, "paidDate": "2021-05-01"},
            {"year": 2, "dueDate": "2022-06-15", "amount": 900,
             "paid": False, "paidDate": None}],
    }
    s = Settings(remind_days=400)  # 先放大窗口便于断言
    ingest_result(conn, case_id, FetchResult(app_no, base_patent, "2024-01-01T00:00:00"), s)
    changed = dict(base_patent)
    changed["legalStatus"] = "失效-未缴年费"
    changed["assignee"] = "乙公司"
    changed["feeRecords"][1]["paid"] = True
    changed["feeRecords"][1]["paidDate"] = "2022-06-10"
    changes = ingest_result(
        conn, case_id, FetchResult(app_no, changed, "2024-02-01T00:00:00"), s)
    fields = {c["field"] for c in changes}
    assert "legalStatus" in fields and "assignee" in fields
    assert "fee_paid_y2" in fields
    # 本次(非首次)变化都应带 detail, 且不含 init
    assert "init" not in fields
    assert all(c["detail"] for c in changes)
    total = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    # 首次 1 条 init + 本次若干条, 本次至少 3 类核心变化
    assert total == 1 + len(changes) and len(changes) >= 3
    # 法律状态变化确实记录了新旧值
    ls = next(c for c in changes if c["field"] == "legalStatus")
    assert ls["old_value"] == "有效" and ls["new_value"] == "失效-未缴年费"


def test_fee_deadline_and_grace():
    due = fee_deadline("2020-06-15", 3)
    assert due == date(2023, 6, 15)
    assert fee_deadline("2020-06-15", 3, advance_days=30) == date(2023, 5, 16)
    # 闰年 2/29 兜底
    assert fee_deadline("2020-02-29", 1) == date(2021, 2, 28)


def test_reminder_levels(conn):
    app_no = _valid_no()
    import_rows(conn, [ImportRow(app_no, "甲")])
    case_id = conn.execute("SELECT id FROM cases").fetchone()[0]
    today_ = date(2024, 5, 1)
    data = {
        "appNo": app_no, "appDate": "2020-06-15",
        "feeRecords": [
            # 50 天后到期 -> due_soon
            {"year": 4, "dueDate": (today_ + timedelta(days=50)).isoformat(),
             "amount": 1200, "paid": False, "paidDate": None},
            # 已过应缴日但在 180 天宽限期内 -> grace
            {"year": 3, "dueDate": (today_ - timedelta(days=30)).isoformat(),
             "amount": 900, "paid": False, "paidDate": None},
            # 超过宽限期 -> overdue
            {"year": 2, "dueDate": (today_ - timedelta(days=200)).isoformat(),
             "amount": 900, "paid": False, "paidDate": None},
            # 已缴, 不进提醒
            {"year": 1, "dueDate": "2021-06-15", "amount": 900,
             "paid": True, "paidDate": "2021-06-01"},
        ]}
    n = refresh_reminders(conn, case_id, data, remind_days=60,
                          grace_days=180, advance_days=0, today_=today_)
    assert n == 3
    levels = {r["fee_year"]: r["level"] for r in conn.execute(
        "SELECT * FROM reminders")}
    assert levels[4] == "due_soon"
    assert levels[3] == "grace"
    assert levels[2] == "overdue"


def test_far_future_fee_not_reminded(conn):
    app_no = _valid_no()
    import_rows(conn, [ImportRow(app_no, "甲")])
    case_id = conn.execute("SELECT id FROM cases").fetchone()[0]
    data = {"appDate": "2020-01-01", "feeRecords": [
        {"year": 10, "dueDate": (date.today() + timedelta(days=400)).isoformat(),
         "amount": 4000, "paid": False, "paidDate": None}]}
    n = refresh_reminders(conn, case_id, data, 60, 180, 0)
    assert n == 0
