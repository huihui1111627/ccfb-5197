# -*- coding: utf-8 -*-
"""本地 mock 专利查询站。

特性:
* /login 登录后下发 session,检索接口必须带有效 cookie;
* /search 页面内嵌 JS 签名函数 sign(),/api/patent 必须带 sign + ts,
  签名算法由页面 JS 计算(采集器只能在浏览器里 evaluate);
* 同 IP 请求过快触发限流,超过阈值封 IP 一段时间,到期自动恢复;
* /admin/* 供测试与演示改变专利状态(模拟状态变化)、推进时间。

环境变量(测试可调小):
MOCK_PORT(默认 5051)、MOCK_RATE_WINDOW(秒,默认 10)、
MOCK_RATE_MAX(窗口内最大请求,默认 5)、MOCK_BAN_SECONDS(封禁秒,默认 60)
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import date, datetime
from functools import wraps

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

from mock_site.seed import build_patents

app = Flask(__name__)
app.secret_key = "mock-patent-site-secret"
SIGN_TOKEN = "page-secret-token-2026"
ADMIN_TOKEN = "admin-token"
TS_TTL_SECONDS = 300

RATE_WINDOW = float(os.environ.get("MOCK_RATE_WINDOW", "10"))
RATE_MAX = int(os.environ.get("MOCK_RATE_MAX", "5"))
BAN_SECONDS = float(os.environ.get("MOCK_BAN_SECONDS", "60"))

_LOCK = threading.RLock()
PATENTS: dict = {}
# ip -> {"hits": [ts...], "banned_until": float|None, "ban_count": n}
_BUCKETS: dict[str, dict] = {}


def reset_state() -> None:
    """测试用: 重置内存库与限流状态。"""
    with _LOCK:
        PATENTS.clear()
        PATENTS.update(build_patents())
        _BUCKETS.clear()


reset_state()


# ---------------------------------------------------------------- 限流
def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?")


def rate_limited(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ip = _client_ip()
        now = time.time()
        with _LOCK:
            b = _BUCKETS.setdefault(ip, {"hits": [], "banned_until": None,
                                         "ban_count": 0})
            if b["banned_until"]:
                if now < b["banned_until"]:
                    retry = int(b["banned_until"] - now)
                    resp = jsonify(code=4030, message=f"IP 已被封禁, {retry}s 后恢复",
                                   retryAfter=retry)
                    resp.status_code = 403
                    return resp
                b["banned_until"] = None
                b["hits"].clear()
            b["hits"] = [t for t in b["hits"] if now - t < RATE_WINDOW]
            b["hits"].append(now)
            if len(b["hits"]) > RATE_MAX:
                b["banned_until"] = now + BAN_SECONDS
                b["ban_count"] += 1
                resp = jsonify(code=4030,
                               message=f"请求过快, 封 IP {int(BAN_SECONDS)}s",
                               retryAfter=int(BAN_SECONDS))
                resp.status_code = 403
                return resp
        return fn(*args, **kwargs)
    return wrapper


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            if request.path.startswith("/api/"):
                return jsonify(code=4010, message="未登录或会话过期"), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def _server_sign(ts: str, app_no: str) -> str:
    """与页面 sign.js 完全一致的服务端镜像实现。"""
    raw = f"{SIGN_TOKEN}|{ts}|{app_no}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 页面
@app.route("/")
def index():
    return redirect(url_for("search_page") if session.get("uid")
                    else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pwd = request.form.get("password", "")
        if user == "agent" and pwd == "agent123":
            session["uid"] = user
            return redirect(url_for("search_page"))
        return render_template("login.html", error="用户名或密码错误")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/search")
@login_required
def search_page():
    return render_template("search.html", user=session.get("uid"))


# ---------------------------------------------------------------- API
@app.route("/api/patent")
@rate_limited
@login_required
def api_patent():
    app_no = request.args.get("appNo", "")
    ts = request.args.get("ts", "")
    sign = request.args.get("sign", "")
    if not app_no or not ts or not sign:
        return jsonify(code=4000, message="缺少 appNo/ts/sign 参数"), 400
    try:
        if abs(time.time() - int(ts)) > TS_TTL_SECONDS:
            return jsonify(code=4001, message="时间戳过期, 请重新签名"), 400
    except ValueError:
        return jsonify(code=4001, message="非法时间戳"), 400
    if sign != _server_sign(ts, app_no):
        return jsonify(code=4002, message="签名校验失败"), 400
    with _LOCK:
        p = PATENTS.get(app_no)
    if not p:
        return jsonify(code=4040, message=f"未找到申请号 {app_no} 的专利"), 404
    return jsonify(code=0, data=p)


# ---------------------------------------------------------------- 管理端
def _admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            return jsonify(code=4031, message="管理端 token 错误"), 403
        return fn(*args, **kwargs)
    return wrapper


@app.route("/admin/list")
@_admin_required
def admin_list():
    with _LOCK:
        return jsonify(code=0, data=list(PATENTS.keys()))


@app.route("/admin/mutate", methods=["POST"])
@_admin_required
def admin_mutate():
    """修改一件专利: 改法律状态/专利权人,或把某年费改为未缴。"""
    body = request.get_json(force=True)
    app_no = body["appNo"]
    with _LOCK:
        p = PATENTS.get(app_no)
        if not p:
            return jsonify(code=4040, message="不存在"), 404
        if "legalStatus" in body:
            p["legalStatus"] = body["legalStatus"]
            p["statusDate"] = body.get("statusDate", date.today().isoformat())
        if "assignee" in body:
            p["assignee"] = body["assignee"]
        if "unpaidYear" in body:
            for f in p["feeRecords"]:
                if f["year"] == int(body["unpaidYear"]):
                    f["paid"], f["paidDate"] = False, None
        return jsonify(code=0, data=p)


@app.route("/admin/add-fee-year", methods=["POST"])
@_admin_required
def admin_add_fee_year():
    """为案件新增一个年费年度(模拟时间推进后产生新的应缴年费)。"""
    body = request.get_json(force=True)
    with _LOCK:
        p = PATENTS.get(body["appNo"])
        if not p:
            return jsonify(code=4040, message="不存在"), 404
        y = int(body["year"])
        if any(f["year"] == y for f in p["feeRecords"]):
            return jsonify(code=4003, message="该年度已存在"), 400
        app_d = datetime.strptime(p["appDate"], "%Y-%m-%d").date()
        due = date(app_d.year + y, app_d.month, app_d.day)
        amount = (body.get("amount") or
                  max(f["amount"] for f in p["feeRecords"]))
        p["feeRecords"].append({"year": y, "dueDate": due.isoformat(),
                                "amount": amount, "paid": False,
                                "paidDate": None})
        p["feeRecords"].sort(key=lambda f: f["year"])
        return jsonify(code=0, data=p)


@app.route("/admin/reset", methods=["POST"])
@_admin_required
def admin_reset():
    reset_state()
    return jsonify(code=0, message="已重置")


@app.route("/admin/bans")
@_admin_required
def admin_bans():
    with _LOCK:
        return jsonify(code=0, data={ip: {"banCount": b["ban_count"],
                                          "bannedUntil": b["banned_until"]}
                                     for ip, b in _BUCKETS.items()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("MOCK_PORT", "5051")),
            debug=False)
