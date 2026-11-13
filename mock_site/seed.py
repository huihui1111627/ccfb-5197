# -*- coding: utf-8 -*-
"""生成 mock 专利库(确定性数据,测试与演示共用)。"""
from __future__ import annotations

import random
from datetime import date, timedelta

from common.checksum import compute_check_digit

# 年费金额表(CNIPA 现行标准的简化版)
FEE_TABLE = {
    "发明": [(3, 900), (6, 1200), (9, 2000), (12, 4000), (15, 6000), (20, 8000)],
    "实用新型": [(3, 600), (5, 900), (8, 1200), (10, 2000)],
    "外观设计": [(3, 600), (5, 900), (8, 1200), (10, 2000), (15, 2000)],
}
TYPE_CODE = {"发明": "1", "实用新型": "2", "外观设计": "3"}
CUSTOMERS = ["华创科技有限公司", "恒远机械股份公司", "云启软件有限公司", "北辰新材料公司"]
STATUSES_BAD = ["失效-未缴年费", "无效宣告", "专利权终止"]
TITLES = ["一种", "基于", "用于"] + ["智能", "高效", "低功耗", "模块化", "复合"] + \
         ["传动装置", "散热结构", "数据处理方法", "制备工艺", "检测系统", "连接组件"]


def _fee_amount(kind: str, year: int) -> int:
    for upper, amount in FEE_TABLE[kind]:
        if year <= upper:
            return amount
    return FEE_TABLE[kind][-1][1]


def make_app_no(year: int, kind: str, serial: int) -> str:
    base = f"{year}{TYPE_CODE[kind]}{serial:07d}"
    return base + compute_check_digit(base)


def build_patents(today: date | None = None) -> dict:
    """构造 40 件 mock 专利,返回 {申请号: 专利dict}。"""
    rnd = random.Random(42)
    today = today or date.today()
    patents: dict[str, dict] = {}
    kinds = ["发明", "实用新型", "外观设计"]
    for i in range(40):
        kind = kinds[i % 3]
        app_year = today.year - rnd.randint(1, 14)
        app_date = date(app_year, rnd.randint(1, 12), rnd.randint(1, 28))
        app_no = make_app_no(app_year, kind, 1000000 + i)
        years_held = max(1, today.year - app_date.year)
        max_year = min(years_held + 1, 20 if kind == "发明" else 10)

        fees = []
        for y in range(1, max_year + 1):
            due = date(app_date.year + y, app_date.month, app_date.day)
            # 默认已缴; 每 5 件留一件当前年度未缴; 已过期未缴的另作失效处理
            paid = not (i % 5 == 0 and y == max_year and due > today)
            fees.append({
                "year": y,
                "dueDate": due.isoformat(),
                "amount": _fee_amount(kind, y),
                "paid": paid,
                "paidDate": (due - timedelta(days=20)).isoformat() if paid else None,
            })

        # 少数案件初始即异常状态
        if i % 11 == 10:
            legal_status, status_date = STATUSES_BAD[i % len(STATUSES_BAD)], \
                (today - timedelta(days=rnd.randint(30, 300))).isoformat()
            for f in fees:
                if f["dueDate"] < status_date:
                    f["paid"] = True
                else:
                    f["paid"], f["paidDate"] = False, None
        else:
            legal_status, status_date = "有效", app_date.isoformat()

        grant_date = (app_date + timedelta(days=rnd.randint(400, 1200))).isoformat() \
            if legal_status == "有效" or rnd.random() > 0.5 else None
        customer = CUSTOMERS[i % len(CUSTOMERS)]
        title = "".join(rnd.sample(TITLES, 3)) + ("专利" if kind != "发明" else "方法")
        patents[app_no] = {
            "appNo": app_no,
            "appDate": app_date.isoformat(),
            "pubNo": f"CN{app_no[:12]}A",
            "title": title,
            "patentType": kind,
            "applicant": customer,
            "inventor": f"发明人{i:02d}",
            "grantDate": grant_date,
            "legalStatus": legal_status,
            "statusDate": status_date,
            "assignee": customer,
            "feeRecords": fees,
        }
    return patents
