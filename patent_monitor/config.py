# -*- coding: utf-8 -*-
"""全局配置(也可被 settings 表覆盖运行期参数)。"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Settings:
    # mock 站
    base_url: str = os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:5051")
    username: str = "agent"
    password: str = "agent123"

    # 采集节奏
    batch_size: int = 20           # 每批任务数
    request_interval: float = 2.5  # 同 IP 两次请求最小间隔(秒), 要大于 mock 窗口/阈值
    batch_pause: float = 5.0       # 两批之间暂停
    max_retries: int = 3           # 普通错误重试
    headless: bool = True

    # 年费提醒(天)
    remind_days: int = 60          # 应缴日前 N 天开始提醒
    grace_days: int = 180          # 年费滞纳期(宽限期,6 个月)
    renew_advance_days: int = 0    # 提前量(默认按标准申请日对应日)

    # 调度
    poll_interval_minutes: int = 360  # 自动轮询间隔

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


DEFAULT_SETTINGS = Settings()
