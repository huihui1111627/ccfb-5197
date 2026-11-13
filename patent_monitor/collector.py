# -*- coding: utf-8 -*-
"""专利查询采集器。

PlaywrightCollector:
* 真实浏览器登录 mock 站, cookie 落盘复用;
* 在 /search 页面上下文里 evaluate 页面 JS 的 sign() 得到签名;
* 用浏览器上下文(共享 cookie)请求 /api/patent;
* 区分 封禁(403, 带 retryAfter)、会话过期(401)、未找到(404) 等异常。

StubCollector: 单元测试替身, 不启动浏览器, 可编排失败/封禁脚本。
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass

from patent_monitor.config import ROOT, Settings
from patent_monitor.db import now


class CollectorError(RuntimeError):
    pass


class AuthExpired(CollectorError):
    pass


class IpBanned(CollectorError):
    def __init__(self, retry_after: int, message: str = ""):
        super().__init__(message or f"IP 被封, {retry_after}s 后恢复")
        self.retry_after = retry_after


class PatentNotFound(CollectorError):
    pass


@dataclass
class FetchResult:
    app_no: str
    data: dict
    fetched_at: str


class BaseCollector:
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def fetch(self, app_no: str) -> FetchResult:
        raise NotImplementedError


class PlaywrightCollector(BaseCollector):
    def __init__(self, settings: Settings, session_path: str | None = None):
        self.s = settings
        self.session_path = session_path or os.path.join(
            ROOT, "data", "browser_session.json")
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False

    def start(self) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.s.headless)
        storage = self.session_path if os.path.exists(self.session_path) else None
        self._context = self._browser.new_context(storage_state=storage)
        self._page = self._context.new_page()
        self._ensure_login()

    def close(self) -> None:
        try:
            if self._context:
                self._context.storage_state(path=self.session_path)
                self._context.close()
        finally:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
            self._logged_in = False

    # ------------------------------------------------------------ 登录
    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        page = self._page
        page.goto(f"{self.s.base_url}/search", wait_until="networkidle")
        if "/login" not in page.url:
            self._logged_in = True
            return
        page.fill("input[name=username]", self.s.username)
        page.fill("input[name=password]", self.s.password)
        page.click("button[type=submit]")
        page.wait_for_url("**/search", timeout=15000)
        page.wait_for_function("typeof window.sign === 'function'")
        self._logged_in = True

    # ------------------------------------------------------------ 抓取
    def fetch(self, app_no: str) -> FetchResult:
        for attempt in (1, 2):
            try:
                return self._fetch_once(app_no)
            except AuthExpired:
                # 会话过期: 清状态重新登录, 只重试一次
                self._logged_in = False
                self._ensure_login()
                if attempt == 2:
                    raise

    def _fetch_once(self, app_no: str) -> FetchResult:
        page = self._page
        if "/search" not in page.url:
            page.goto(f"{self.s.base_url}/search", wait_until="networkidle")
        # 签名必须由页面 JS 计算
        ts = str(int(time.time()))
        sign = page.evaluate(
            "([ts, appNo]) => window.sign(ts, appNo)", [ts, app_no])
        url = (f"{self.s.base_url}/api/patent?appNo={app_no}&ts={ts}"
               f"&sign={sign}")
        resp = self._context.request.get(url)
        if resp.status == 401:
            raise AuthExpired("会话过期, 需要重新登录")
        body = resp.json()
        if resp.status == 403 and body.get("code") == 4030:
            raise IpBanned(int(body.get("retryAfter", 60)), body.get("message", ""))
        if resp.status == 404:
            raise PatentNotFound(body.get("message", "未找到专利"))
        if resp.status != 200 or body.get("code") != 0:
            raise CollectorError(f"接口异常 status={resp.status} body={body}")
        return FetchResult(app_no, body["data"], now())


class StubCollector(BaseCollector):
    """按脚本返回结果的测试替身。

    script: deque, 元素为
      dict            -> 正常返回该专利数据
      Exception 实例  -> 抛出该异常
    未编排到的 app_no 若在 patents 里则正常返回, 否则 PatentNotFound。
    """

    def __init__(self, patents: dict | None = None, script: list | None = None):
        self.patents = patents or {}
        self.script = deque(script or [])
        self.calls: list[str] = []
        self.login_count = 0

    def fetch(self, app_no: str) -> FetchResult:
        self.calls.append(app_no)
        if self.script:
            item = self.script.popleft()
            if isinstance(item, Exception):
                if isinstance(item, AuthExpired):
                    self.login_count += 1
                raise item
            return FetchResult(app_no, item, now())
        data = self.patents.get(app_no)
        if data is None:
            raise PatentNotFound(f"stub 未找到 {app_no}")
        return FetchResult(app_no, json.loads(json.dumps(data)), now())
