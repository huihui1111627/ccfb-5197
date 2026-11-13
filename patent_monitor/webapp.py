# -*- coding: utf-8 -*-
"""专利法律状态监控 Web 控制台。

五个页面: /cases 案件清单、/tasks 采集任务、/changes 状态变化、
/reminders 期限提醒、/settings 设置; 另含导入、导出 Excel、手动触发轮询。
"""
from __future__ import annotations

import io
import os
import threading

from flask import (Flask, flash, redirect, render_template, request,
                   send_file, url_for)
from openpyxl import Workbook

from patent_monitor.db import (connect, init_db, load_settings,
                               save_settings)
from patent_monitor.importer import (distinct_customers,
                                     distinct_statuses, import_rows,
                                     list_cases, parse_file)
from patent_monitor.scheduler import Scheduler

app = Flask(__name__)
app.secret_key = "patent-monitor-console"
DB = os.environ.get("MONITOR_DB")

_conn = None
_scheduler: Scheduler | None = None
_poll_lock = threading.Lock()
_poll_running = False


def get_conn():
    global _conn
    if _conn is None:
        _conn = connect(DB)
        init_db(_conn)
    return _conn


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(get_conn())
    return _scheduler


LEVEL_LABELS = {"due_soon": "即将到期", "grace": "滞纳期",
                "overdue": "已超期", "normal": "正常"}
TASK_LABELS = {"pending": "待采集", "in_progress": "采集中",
               "done": "已完成", "failed": "失败",
               "banned_wait": "封禁等待"}


# ---------------------------------------------------------------- 案件清单
@app.route("/")
def root():
    return redirect(url_for("cases_page"))


@app.route("/cases")
def cases_page():
    conn = get_conn()
    customer = request.args.get("customer", "")
    status = request.args.get("status", "")
    due_level = request.args.get("due_level", "")
    q = request.args.get("q", "")
    page = max(1, int(request.args.get("page", 1)))
    per_page = 20
    filters = {"customer": customer, "status": status,
               "due_level": due_level, "q": q}
    total = len(list_cases(conn, limit=10_000_000, offset=0, **filters))
    rows = list_cases(conn, limit=per_page, offset=(page - 1) * per_page,
                      **filters)
    return render_template(
        "cases.html", rows=rows, total=total, page=page, per_page=per_page,
        customers=distinct_customers(conn),
        statuses=distinct_statuses(conn),
        levels=LEVEL_LABELS, filters=filters)


@app.route("/cases/import", methods=["POST"])
def cases_import():
    f = request.files.get("file")
    if not f:
        flash("请选择文件", "error")
        return redirect(url_for("cases_page"))
    try:
        rows = parse_file(f.filename, f.read())
        rep = import_rows(get_conn(), rows)
    except Exception as e:  # noqa: BLE001
        flash(f"导入失败: {e}", "error")
        return redirect(url_for("cases_page"))
    flash(f"共 {rep.total} 行: 新增 {rep.inserted}, 重复 {rep.duplicated}, "
          f"校验不过 {rep.invalid}", "ok")
    if rep.invalid_rows:
        for r in rep.invalid_rows[:10]:
            flash(f"校验拦截: {r['raw']} — {r['reason']}"
                  + (f"(建议 {r['suggest']})" if r["suggest"] else ""),
                  "warn")
    return redirect(url_for("cases_page"))


@app.route("/cases/export")
def cases_export():
    conn = get_conn()
    filters = {k: request.args.get(k, "") for k in
               ("customer", "status", "due_level", "q")}
    rows = list_cases(conn, limit=100_000, offset=0, **filters)
    wb = Workbook()
    ws = wb.active
    ws.title = "案件清单"
    ws.append(["申请号", "客户", "备注", "校验", "法律状态",
               "最近采集", "提醒级别", "应缴日", "年费年度"])
    for r in rows:
        ws.append([r["app_no"], r["customer"], r["case_alias"],
                   "通过" if r["check_valid"] else f"不通过:{r['check_reason']}",
                   r["legal_status"] or "未采集", r["last_poll"] or "",
                   LEVEL_LABELS.get(r["due_level"], ""),
                   r["due_date"] or "", r["fee_year"] or ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="patent_cases.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


# ---------------------------------------------------------------- 采集任务
@app.route("/tasks")
def tasks_page():
    conn = get_conn()
    status = request.args.get("status", "")
    sql = ("SELECT t.*, c.app_no, c.customer, c.valid FROM tasks t "
           "JOIN cases c ON c.id=t.case_id WHERE 1=1")
    args = []
    if status:
        sql += " AND t.status=?"
        args.append(status)
    sql += " ORDER BY t.case_id"
    tasks = conn.execute(sql, args).fetchall()
    invalid_cases = conn.execute(
        "SELECT * FROM cases WHERE valid=0 ORDER BY id DESC").fetchall()
    logs = conn.execute(
        "SELECT * FROM poll_logs ORDER BY id DESC LIMIT 10").fetchall()
    return render_template("tasks.html", tasks=tasks,
                           labels=TASK_LABELS, logs=logs,
                           invalid_cases=invalid_cases,
                           filter_status=status,
                           running=_poll_running)


@app.route("/tasks/run", methods=["POST"])
def tasks_run():
    global _poll_running
    if not _poll_lock.acquire(blocking=False):
        flash("已有一轮采集在运行中", "warn")
        return redirect(url_for("tasks_page"))
    _poll_running = True

    def _work():
        global _poll_running
        try:
            stats = get_scheduler().run_poll()
            print(f"[web] 轮询完成: {stats}")
        except Exception as e:  # noqa: BLE001
            print(f"[web] 轮询失败: {e}")
        finally:
            _poll_running = False
            _poll_lock.release()

    threading.Thread(target=_work, daemon=True).start()
    flash("已触发一轮采集, 可在本页查看进度", "ok")
    return redirect(url_for("tasks_page"))


@app.route("/tasks/reset", methods=["POST"])
def tasks_reset():
    """失败/封禁任务重置为 pending, 方便人工干预后重采。"""
    conn = get_conn()
    conn.execute(
        "UPDATE tasks SET status='pending', last_error=NULL, "
        "next_run_at=NULL WHERE status IN ('failed','banned_wait')")
    conn.commit()
    flash("已重置失败与封禁中的任务", "ok")
    return redirect(url_for("tasks_page"))


# ---------------------------------------------------------------- 状态变化
@app.route("/changes")
def changes_page():
    conn = get_conn()
    case_id = request.args.get("case_id", "")
    sql = ("SELECT ch.*, c.app_no, c.customer FROM changes ch "
           "JOIN cases c ON c.id=ch.case_id WHERE 1=1")
    args = []
    if case_id:
        sql += " AND ch.case_id=?"
        args.append(case_id)
    sql += " ORDER BY ch.id DESC LIMIT 500"
    changes = conn.execute(sql, args).fetchall()
    return render_template("changes.html", changes=changes,
                           case_id=case_id)


@app.route("/cases/<int:case_id>")
def case_detail(case_id: int):
    conn = get_conn()
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        flash("案件不存在", "error")
        return redirect(url_for("cases_page"))
    snapshots = conn.execute(
        "SELECT * FROM snapshots WHERE case_id=? ORDER BY id DESC LIMIT 20",
        (case_id,)).fetchall()
    changes = conn.execute(
        "SELECT * FROM changes WHERE case_id=? ORDER BY id DESC",
        (case_id,)).fetchall()
    reminders = conn.execute(
        "SELECT * FROM reminders WHERE case_id=? ORDER BY due_date",
        (case_id,)).fetchall()
    return render_template("case_detail.html", case=case,
                           snapshots=snapshots, changes=changes,
                           reminders=reminders, levels=LEVEL_LABELS)


# ---------------------------------------------------------------- 期限提醒
@app.route("/reminders")
def reminders_page():
    conn = get_conn()
    level = request.args.get("level", "")
    sql = ("SELECT r.*, c.app_no, c.customer, c.case_alias FROM reminders r "
           "JOIN cases c ON c.id=r.case_id WHERE r.paid=0")
    args = []
    if level:
        sql += " AND r.level=?"
        args.append(level)
    sql += " ORDER BY r.due_date"
    reminders = conn.execute(sql, args).fetchall()
    return render_template("reminders.html", reminders=reminders,
                           labels=LEVEL_LABELS, filter_level=level,
                           s_grace=load_settings(conn).grace_days)


# ---------------------------------------------------------------- 设置
@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    conn = get_conn()
    if request.method == "POST":
        data = {}
        for key in ("base_url", "username", "password", "headless",
                    "batch_size", "request_interval", "batch_pause",
                    "max_retries", "remind_days", "grace_days",
                    "renew_advance_days", "poll_interval_minutes"):
            val = request.form.get(key)
            if val is None:
                continue
            if key in ("headless",):
                data[key] = val == "on"
            elif key in ("batch_size", "max_retries", "remind_days",
                         "grace_days", "renew_advance_days",
                         "poll_interval_minutes"):
                data[key] = int(val)
            elif key in ("request_interval", "batch_pause"):
                data[key] = float(val)
            else:
                data[key] = val.strip()
        save_settings(conn, data)
        flash("设置已保存", "ok")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", s=load_settings(conn))


if __name__ == "__main__":
    init_db(get_conn())
    app.run(host="127.0.0.1", port=int(os.environ.get("WEB_PORT", "5050")),
            debug=False)
