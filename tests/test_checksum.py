# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.checksum import compute_check_digit, normalize  # noqa: E402


def test_check_digit_roundtrip():
    base = "202010123456"
    num = base + compute_check_digit(base)
    assert normalize(num).ok
    assert normalize(num).normalized == num


def test_messy_formats_normalized():
    base = "202010123456"
    num = base + compute_check_digit(base)
    variants = [
        f"ZL{num}",
        f"{num[:4]}.{num[4]}.{num[5:12]}.{num[12]}",
        f"ZL {num[:4]} {num[4]} {num[5:12]} {num[12]}",
        "ｚｌ" + num,
        num.replace("1", "１", 1),
    ]
    for v in variants:
        r = normalize(v)
        assert r.ok, v
        assert r.normalized == num


def test_bad_check_digit_rejected():
    base = "202010123456"
    bad = base + ("0" if compute_check_digit(base) != "0" else "1")
    r = normalize(bad)
    assert not r.ok
    assert "校验位错误" in r.reason


def test_missing_check_digit_flagged_with_suggestion():
    r = normalize("202010123456")
    assert not r.ok
    assert r.normalized == "202010123456" + compute_check_digit("202010123456")
    assert "缺少校验位" in r.reason


def test_unrecognizable():
    for raw in ("PATENT-001", "", "  ", "20201012345"):
        assert not normalize(raw).ok


def test_old_style_patent():
    base = "85100123"
    num = base + compute_check_digit(base)
    r = normalize(num)
    assert r.ok and r.style == "old" and r.normalized == num
    # 旧式错校验位
    wrong = base + ("X" if r.check_digit != "X" else "0")
    assert not normalize(wrong).ok


def test_x_check_digit():
    # 构造一个校验位恰为 X 的主体(暴力搜索, 仅用于测试)
    found = None
    for serial in range(1_000_000, 1_010_000):
        base = f"20201{serial:07d}"
        if compute_check_digit(base) == "X":
            found = base
            break
    assert found, "未找到 X 校验位样本"
    assert len(found) == 12
    r = normalize(found + "X")
    assert r.ok and r.check_digit == "X"
