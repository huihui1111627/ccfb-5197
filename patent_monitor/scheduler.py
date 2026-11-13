# -*- coding: utf-8 -*-
"""轮询调度: 分批限速、封禁等待、断点续采、轮询日志。

断点语义: 每轮轮询在 poll_logs 开一条记录, cursor_pos 持久化"下一个待处理
下标"。进程在任意时刻被杀, 下一次 run_poll 会找到未完成的日志, 从游标继续;
整轮正常结束后日志收尾, 任务重新置 pending 进入下一轮。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta

from patent_monitor.collector import (AuthExpired, CollectorError,
                                       IpBanned, PatentNotFound,
                                       PlaywrightCollector)
from patent_monitor.db import load_settings, now
from patent_monitor.monitor import ingest_result

TASK_SQL = ("SELECT t.id tid, t.case_id, t.status, t.attempts, "
            "t.next_run_at, c.app_no FROM tasks t JOIN cases c "
            "ON c.id=t.case_id WHERE c.valid=1 ORDER BY t.case_id")


class Scheduler:
    def __init__(self, conn: sqlite3.Connection,
                 collector_factory=None, sleeper=None, clock=None):
        """collector_factory: () -> BaseCollector, 默认真 Playwright。
        sleeper: 可注入的 sleep; clock: 可注入的 now(datetime), 测试用。"""
        self.conn = conn
        self._factory = collector_factory or (
            lambda: PlaywrightCollector(load_settings(conn)))
        self._sleep = sleeper or time.sleep
        self._clock = clock or datetime.now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 单轮
    def run_poll(self) -> dict:
        s = load_settings(self.conn)
        # 找未完成的轮次(断点续采), 否则开新轮
        open_log = self.conn.execute(
            "SELECT * FROM poll_logs WHERE finished_at IS NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if open_log:
            log_id, cursor = open_log["id"], open_log["cursor_pos"]
            note = "从断点恢复"
        else:
            # 新一轮: 把上轮完成的任务重新置 pending, 失败/封禁态保持人工可干预
            self.conn.execute(
                "UPDATE tasks SET status='pending', next_run_at=NULL, "
                "attempts=0 WHERE status='done'")
            self.conn.commit()
            log_id = self.conn.execute(
                "INSERT INTO poll_logs(started_at,cursor_pos) VALUES(?,0)",
                (now(),)).lastrowid
            self.conn.commit()
            cursor, note = 0, ""

        stats = {"total": 0, "success": 0, "failed": 0, "banned": 0,
                 "skipped_banned": 0}
        tasks = self.conn.execute(TASK_SQL).fetchall()
        if not tasks:
            self._finish_log(log_id, stats, "无待采集任务")
            return stats
        stats["total"] = len(tasks)
        cursor = min(cursor, len(tasks))

        collector = self._factory()
        collector.start()
        try:
            self._drain(tasks, cursor, collector, s, stats, log_id)
        except BaseException:
            # 中断/崩溃: 游标已逐条持久化, 保留未完成日志供下次断点续采,
            # 只关浏览器, 不做收尾
            collector.close()
            raise
        collector.close()
        self._finish_log(log_id, stats, note)
        return stats

    def _drain(self, tasks, cursor, collector, s, stats, log_id) -> None:
        """多趟扫描: 封禁/退避中的任务先跳过, 等最早解封时间到了再回扫。

        每趟结束都以数据库当前状态判断是否还需要回扫, 不依赖内存快照。
        """
        max_passes = 6
        for _ in range(max_passes):
            if self._stop.is_set():
                return
            ran_in_pass = 0
            idx = cursor
            while idx < len(tasks):
                if self._stop.is_set():
                    return
                t = tasks[idx]
                runnable, _wait = self._is_runnable(t)
                if not runnable:
                    stats["skipped_banned"] += 1
                    idx += 1
                    cursor = idx
                    self._save_cursor(log_id, cursor)
                    continue
                # 每处理满一批, 批间暂停
                if ran_in_pass and ran_in_pass % s.batch_size == 0:
                    self._sleep(s.batch_pause)
                ok = self._run_one(t, collector, s, stats)
                ran_in_pass += 1
                idx += 1
                cursor = idx
                self._save_cursor(log_id, cursor)
                if ok:
                    self._sleep(s.request_interval)

            # 以 DB 实际状态决定下一步
            open_ended = self.conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN "
                "('done','failed')").fetchone()[0]
            if open_ended == 0:
                return
            waiting = self.conn.execute(
                "SELECT MIN(next_run_at) nr FROM tasks "
                "WHERE next_run_at IS NOT NULL AND status IN "
                "('banned_wait','pending')").fetchone()
            if not waiting["nr"]:
                # 有未完成任务但没有可等待的恢复时间 -> 结束本轮
                return
            wait_s = max(0.0, (datetime.fromisoformat(waiting["nr"])
                               - self._clock()).total_seconds())
            if wait_s > 0:
                # 用注入的 sleeper: 生产环境为真实 sleep, 测试可虚拟推进
                self._sleep(wait_s + 0.2)
                if self._stop.is_set():
                    return
            # 回扫: 重新取任务, 游标归零
            cursor = 0
            tasks = self.conn.execute(TASK_SQL).fetchall()

    def _is_runnable(self, task_row) -> tuple[bool, float | None]:
        if task_row["status"] == "done":
            return False, None
        nr = task_row["next_run_at"]
        if nr and datetime.fromisoformat(nr) > self._clock():
            # 封禁等待或重试退避中
            return False, (datetime.fromisoformat(nr)
                           - self._clock()).total_seconds()
        return True, None

    def _run_one(self, t, collector, s, stats) -> bool:
        """True=正常消耗一次请求配额(用于限速); 封禁不消耗。"""
        tid, case_id, app_no = t["tid"], t["case_id"], t["app_no"]
        self.conn.execute(
            "UPDATE tasks SET status='in_progress', attempts=attempts+1, "
            "last_run_at=? WHERE id=?", (now(), tid))
        self.conn.commit()
        try:
            result = collector.fetch(app_no)
        except IpBanned as e:
            stats["banned"] += 1
            recover = (self._clock()
                       + timedelta(seconds=e.retry_after)).isoformat(
                timespec="seconds")
            self.conn.execute(
                "UPDATE tasks SET status='banned_wait', last_error=?, "
                "next_run_at=? WHERE id=?", (str(e), recover, tid))
            self.conn.commit()
            return False
        except PatentNotFound as e:
            stats["failed"] += 1
            self.conn.execute(
                "UPDATE tasks SET status='failed', last_error=? WHERE id=?",
                (str(e), tid))
            self.conn.commit()
            return True
        except (CollectorError, AuthExpired, Exception) as e:  # noqa: BLE001
            stats["failed"] += 1
            attempts = t["attempts"] + 1
            if attempts >= s.max_retries:
                self.conn.execute(
                    "UPDATE tasks SET status='failed', last_error=? "
                    "WHERE id=?", (f"重试耗尽: {e}", tid))
            else:
                self.conn.execute(
                    "UPDATE tasks SET status='pending', last_error=?, "
                    "next_run_at=? WHERE id=?",
                    (str(e), (self._clock() + timedelta(minutes=5))
                     .isoformat(timespec="seconds"), tid))
            self.conn.commit()
            return True

        ingest_result(self.conn, case_id, result, s)
        stats["success"] += 1
        self.conn.execute(
            "UPDATE tasks SET status='done', last_error=NULL, "
            "next_run_at=NULL WHERE id=?", (tid,))
        self.conn.commit()
        return True

    def _save_cursor(self, log_id: int, pos: int) -> None:
        self.conn.execute(
            "UPDATE poll_logs SET cursor_pos=? WHERE id=?", (pos, log_id))
        self.conn.commit()

    def _finish_log(self, log_id, stats, note) -> None:
        # 收尾只关日志; done->pending 的轮次重置在新一轮开始时做,
        # 这样本轮结束后任务状态仍可被观测, 也不影响断点恢复
        self.conn.execute(
            "UPDATE poll_logs SET finished_at=?, total=?, success=?, "
            "failed=?, banned=?, cursor_pos=0, note=? WHERE id=?",
            (now(), stats["total"], stats["success"], stats["failed"],
             stats["banned"], note, log_id))
        self.conn.commit()

    # ------------------------------------------------------------ 循环模式
    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop():
            while not self._stop.is_set():
                try:
                    self.run_poll()
                except Exception as e:  # noqa: BLE001
                    print(f"[scheduler] 轮询异常: {e}")
                self._stop.wait(load_settings(self.conn)
                                .poll_interval_minutes * 60)

        self._thread = threading.Thread(target=_loop, daemon=True,
                                        name="poll-loop")
        self._thread.start()
