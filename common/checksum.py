# -*- coding: utf-8 -*-
"""中国专利申请号归一化与校验位。

支持两种制式(均符合 CNIPA 校验规则,校验位由前 8/12 位按
权重 2,3,4,5,6,7,8,9,2,3,4,5 加权求和后 mod 11 计算):

* 新式(1993 年起,13 位): YYYY + 类型(1) + 流水(7) + 校验位(1)
  例: 2020101234567
* 旧式(1985-1992,9 位): YY + 类型(1) + 流水(5) + 校验位(1)
  例: 851001238
"""
from __future__ import annotations

import re
from dataclasses import dataclass

WEIGHTS = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)

# 1 发明  2 实用新型  3 外观设计(旧)  8 PCT 进国家阶段发明  9 PCT 进国家阶段实用新型
# J 进入国家阶段的发明(新)  S 实用新型(旧后缀)
TYPE_CHARS = set("12389JS")


@dataclass
class Normalized:
    ok: bool
    """格式本身是否可识别。"""
    normalized: str | None
    """统一后的纯数字/带 X 的申请号(无分隔符)。"""
    base: str | None
    """去掉校验位后的主体(12 或 8 位)。"""
    check_digit: str | None
    style: str | None
    """'new' 或 'old'。"""
    reason: str = ""


def compute_check_digit(base: str) -> str:
    """对 8 位(旧式)或 12 位(新式)主体计算校验位。"""
    if not re.fullmatch(r"\d{8}|\d{12}", base):
        raise ValueError("主体必须是 8 或 12 位数字")
    weights = WEIGHTS if len(base) == 12 else WEIGHTS[:8]
    total = sum(int(ch) * w for ch, w in zip(base, weights))
    d = (11 - total % 11) % 11
    return "X" if d == 10 else str(d)


def normalize(raw: str) -> Normalized:
    """把五花八门的输入整理成标准申请号并验校验位。

    接受: 全角/半角分隔符(. － - 空格 /)、可选 ZL 前缀、旧式两位数年份。
    校验不过时 ok=False 并给出 reason,调用方不应发请求。
    """
    if raw is None:
        return Normalized(False, None, None, None, None, "空值")
    s = str(raw).strip()
    if not s:
        return Normalized(False, None, None, None, None, "空值")

    # 全角转半角,去空白和常见分隔符,大写
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
                for c in s)
    s = re.sub(r"\s+", "", s)
    s = s.replace("．", ".").replace("－", "-").replace("—", "-")
    s = re.sub(r"[./_]", "", s)
    s = s.upper()
    if s.startswith("ZL"):
        s = s[2:]

    # 旧式: 85 1 00123 8  / 也允许 9 位里末位是 X
    m_old = re.fullmatch(r"(\d{2})([12389JS])(\d{5})([0-9X])", s)
    if m_old:
        year2, typ, serial, given = m_old.groups()
        if typ not in TYPE_CHARS:
            return Normalized(False, None, None, None, None, f"未知专利类型 {typ}")
        base = f"{year2}{typ}{serial}"
        expect = compute_check_digit(base)
        if given != expect:
            return Normalized(False, None, base, given, "old",
                              f"校验位错误: 应为 {expect}, 实际 {given}")
        return Normalized(True, base + expect, base, expect, "old")

    # 新式: 2020 1 0123456 7(末位可能缺失)
    m_new = re.fullmatch(r"(\d{4})([12389JS])(\d{7})([0-9X])?", s)
    if m_new:
        year4, typ, serial, given = m_new.groups()
        if int(year4) < 1993 or int(year4) > 2100:
            return Normalized(False, None, None, None, None,
                              f"年份 {year4} 超出新式申请号范围")
        base = f"{year4}{typ}{serial}"
        if given is None:
            expect = compute_check_digit(base)
            return Normalized(False, base + expect, base, expect, "new",
                              "缺少校验位(已按主体推算, 请核对)")
        expect = compute_check_digit(base)
        if given != expect:
            return Normalized(False, None, base, given, "new",
                              f"校验位错误: 应为 {expect}, 实际 {given}")
        return Normalized(True, base + expect, base, expect, "new")

    return Normalized(False, None, None, None, None,
                      f"无法识别的申请号格式: {raw!r}")


def is_valid(raw: str) -> bool:
    return normalize(raw).ok
