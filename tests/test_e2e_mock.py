# -*- coding: utf-8 -*-
"""端到端: 真实 Chromium 打 mock 站, 覆盖登录/JS签名/限流封禁/变异差异。

跑法: pytest tests/test_e2e_mock.py
mock 限流参数调小: 窗口 5s 内 3 次即封 8s。
"""
import os
import sys
import time

import pytest

pytest.importorskip("playwright")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patent_monitor.collector import (IpBanned,  # noqa: E402
                                      PlaywrightCollector)
from patent_monitor.config import Settings  # noqa: E402
from patent_monitor.db import connect, init_db, save_settings  # noqa: E402
from patent_monitor.importer import ImportRow, import_rows  # noqa: E402
from patent_monitor.scheduler import Scheduler  # noqa: E402

MOCK_PORT = 5091
BASE_URL = f"http://127.0.0.1:{MOCK_PORT}"
ADMIN = {"X-Admin-Token": "admin-token"}


@pytest.fixture(scope="module")
def mock_server():
    os.environ.update({
        "MOCK_PORT": str(MOCK_PORT),
        "MOCK_RATE_WINDOW": "5",
        "MOCK_RATE_MAX": "3",
        "MOCK_BAN_SECONDS": "8",
    })
    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    from mock_site.app import app, reset_state
    import threading
    reset_state()
    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=MOCK_PORT,
                               use_reloader=False), daemon=True)
    t.start()
    # 等服务起来
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE_URL + "/login", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    yield app


@pytest.fixture(autouse=True)
def _reset_mock():
    """每个用例前重置 mock 库与 IP 限流桶, 避免封禁跨用例残留。"""
    from mock_site.app import reset_state
    reset_state()
    yield


@pytest.fixture()
def settings(tmp_path):
    return Settings(base_url=BASE_URL, headless=True,
                    request_interval=0.0, batch_pause=0.0, batch_size=50,
                    max_retries=2)


def _app_nos(n):
    # 直接取 mock 库中真实存在的申请号(seed 的申请年份是随机的)
    from mock_site.app import PATENTS
    return list(PATENTS.keys())[:n]


def test_login_and_js_signature_fetch(mock_server, settings, tmp_path):
    ss = str(tmp_path / "ss.json")
    s = Settings(base_url=BASE_URL, headless=True)
    with PlaywrightCollector(s, session_path=ss) as col:
        app_no = _app_nos(1)[0]
        r = col.fetch(app_no)
        assert r.data["appNo"] == app_no
        assert "feeRecords" in r.data and r.data["legalStatus"]
    # storage_state 已落盘, 第二次复用会话不再走登录表单
    with PlaywrightCollector(s, session_path=ss) as col2:
        r2 = col2.fetch(_app_nos(2)[1])
        assert r2.data["appNo"]


def test_rate_limit_ban_and_auto_recover(mock_server, settings, tmp_path):
    s = Settings(base_url=BASE_URL, headless=True)
    ss = str(tmp_path / "ss2.json")
    nos = _app_nos(6)
    with PlaywrightCollector(s, session_path=ss) as col:
        banned = False
        got = 0
        for no in nos:
            try:
                col.fetch(no)
                got += 1
            except IpBanned as e:
                banned = True
                assert e.retry_after > 0
                break
        assert banned, "快速请求应触发封禁"
        # 封禁期内仍被拒
        with pytest.raises(IpBanned):
            col.fetch(nos[-1])
        # 等到自动解封后可恢复
        time.sleep(9)
        r = col.fetch(nos[-1])
        assert r.data["appNo"] == nos[-1]


def test_scheduler_e2e_with_mutation(mock_server, tmp_path):
    db = str(tmp_path / "e2e.db")
    conn = connect(db)
    init_db(conn)
    save_settings(conn, {"base_url": BASE_URL, "headless": True,
                         # 尊重 mock 限流(5s 窗口/3 次): 真实间隔, 验证端到端限速节奏
                         "request_interval": 2.0, "batch_pause": 1.0,
                         "batch_size": 100, "max_retries": 2})
    nos = _app_nos(5)
    import_rows(conn, [ImportRow(no, "客户X") for no in nos])

    import requests
    ss3 = str(tmp_path / "ss3.json")
    factory = lambda: PlaywrightCollector(  # noqa: E731
        Settings(base_url=BASE_URL, headless=True), session_path=ss3)
    sch = Scheduler(conn, factory, sleeper=lambda s: time.sleep(s))
    stats = sch.run_poll()
    assert stats["success"] == 5
    assert stats["banned"] >= 0

    # 通过管理端把第一件专利改成"无效宣告", 再跑一轮, 应产生法律状态差异
    target = nos[0]
    resp = requests.post(
        f"{BASE_URL}/admin/mutate",
        json={"appNo": target, "legalStatus": "无效宣告"}, headers=ADMIN)
    assert resp.json()["code"] == 0
    sch.run_poll()
    ch = conn.execute(
        "SELECT * FROM changes WHERE field='legalStatus' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert ch and ch["new_value"] == "无效宣告"
    conn.close()
