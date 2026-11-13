# -*- coding: utf-8 -*-
"""调度器单元测试: 用 StubCollector, 不启浏览器, 虚拟时钟即时推进。"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.checksum import compute_check_digit  # noqa: E402
from patent_monitor.collector import (AuthExpired, IpBanned,  # noqa: E402
                                      StubCollector)
from patent_monitor.db import connect, init_db, save_settings  # noqa: E402
from patent_monitor.importer import ImportRow, import_rows  # noqa: E402
from patent_monitor.scheduler import Scheduler  # noqa: E402


class FakeClock:
    """虚拟时钟: sleep 只推进时钟, 不阻塞。"""

    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture()
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_db(c)
    save_settings(c, {"request_interval": 0.0, "batch_pause": 0.0,
                      "batch_size": 3, "max_retries": 2})
    yield c
    c.close()


def _no(i):
    base = f"20201{1000000 + i:07d}"
    return base + compute_check_digit(base)


def _patent(app_no):
    return {"appNo": app_no, "appDate": "2020-03-01", "title": "t",
            "patentType": "发明", "grantDate": None, "legalStatus": "有效",
            "statusDate": "2020-03-01", "assignee": "甲",
            "feeRecords": [{"year": 1, "dueDate": "2021-03-01",
                            "amount": 900, "paid": True, "paidDate": "x"}]}


def _seed_cases(conn, n):
    import_rows(conn, [ImportRow(_no(i), f"客户{i}") for i in range(n)])


def test_happy_path_all_done(conn):
    _seed_cases(conn, 5)
    patents = {_no(i): _patent(_no(i)) for i in range(5)}
    clk = FakeClock(datetime(2026, 1, 1))
    sch = Scheduler(conn, lambda: StubCollector(patents),
                    sleeper=clk.sleep, clock=clk.now)
    stats = sch.run_poll()
    assert stats["success"] == 5 and stats["failed"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == 5
    log = conn.execute("SELECT * FROM poll_logs").fetchone()
    assert log["finished_at"] and log["cursor_pos"] == 0


def test_not_found_marks_failed(conn):
    _seed_cases(conn, 2)
    clk = FakeClock(datetime(2026, 1, 1))
    sch = Scheduler(conn, lambda: StubCollector({}),
                    sleeper=clk.sleep, clock=clk.now)
    stats = sch.run_poll()
    assert stats["failed"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='failed'").fetchone()[0] == 2


def test_ip_ban_wait_and_resume(conn):
    _seed_cases(conn, 4)
    patents = {_no(i): _patent(_no(i)) for i in range(4)}
    # 第 1 个请求即封 60s, 之后恢复正常
    stub = StubCollector(patents, script=[IpBanned(60, "封")])
    clk = FakeClock(datetime(2026, 1, 1))
    slept = []

    def recording_sleep(s):
        slept.append(s)
        clk.sleep(s)

    sch = Scheduler(conn, lambda: stub,
                    sleeper=recording_sleep, clock=clk.now)
    stats = sch.run_poll()
    # 第 1 个请求触发一次封禁; 虚拟等待解封后回扫, 4 件最终全部成功
    assert stats["banned"] == 1 and stats["success"] == 4
    # 发生了约 60s 的虚拟等待(解封后回扫)
    assert any(abs(s - 60.2) < 0.5 for s in slept)
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='banned_wait'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == 4


def test_retry_then_success(conn):
    _seed_cases(conn, 2)
    patents = {_no(i): _patent(_no(i)) for i in range(2)}
    # 第一个任务先 AuthExpired(进入退避), 虚拟时钟推进后回扫成功
    stub = StubCollector(patents, script=[AuthExpired("过期")])
    clk = FakeClock(datetime(2026, 1, 1))
    sch = Scheduler(conn, lambda: stub,
                    sleeper=clk.sleep, clock=clk.now)
    stats = sch.run_poll()
    assert stats["success"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == 2


def test_resume_from_cursor_after_interruption(conn):
    _seed_cases(conn, 6)
    patents = {_no(i): _patent(_no(i)) for i in range(6)}

    class StopAfterTwo(StubCollector):
        def __init__(self):
            super().__init__(patents)
            self.n = 0

        def fetch(self, app_no):
            self.n += 1
            if self.n == 3:
                raise KeyboardInterrupt("模拟进程被杀")
            return super().fetch(app_no)

    clk = FakeClock(datetime(2026, 1, 1))
    sch = Scheduler(conn, StopAfterTwo, sleeper=clk.sleep, clock=clk.now)
    with pytest.raises(KeyboardInterrupt):
        sch.run_poll()
    # 未完成轮次保留游标
    open_log = conn.execute(
        "SELECT * FROM poll_logs WHERE finished_at IS NULL").fetchone()
    assert open_log and open_log["cursor_pos"] == 2
    done = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    assert done == 2

    # 新进程: 从游标继续, 剩余 4 件全部完成
    sch2 = Scheduler(conn, lambda: StubCollector(patents),
                     sleeper=clk.sleep, clock=clk.now)
    stats = sch2.run_poll()
    assert stats["success"] == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM poll_logs WHERE finished_at IS NULL"
    ).fetchone()[0] == 0


def test_next_round_reopens_all_tasks(conn):
    _seed_cases(conn, 3)
    patents = {_no(i): _patent(_no(i)) for i in range(3)}
    clk = FakeClock(datetime(2026, 1, 1))
    sch = Scheduler(conn, lambda: StubCollector(patents),
                    sleeper=clk.sleep, clock=clk.now)
    sch.run_poll()
    # 本轮收尾后任务保持 done, 状态可观测
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == 3
    # 下一轮开始时统一重置为 pending 并再次全部成功
    stats = sch.run_poll()
    assert stats["success"] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM poll_logs").fetchone()[0] == 2
