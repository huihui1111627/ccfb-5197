# -*- coding: utf-8 -*-
"""生成演示用案件清单 data/sample_cases.csv。

取 mock 库的真实申请号(保证查得到), 再人为制造:
* 点分/空格/ZL/全角等五花八门写法;
* 校验位错误、缺校验位、无法识别的脏行(应被拦截,不发请求);
* 一个 mock 库不存在但校验合法的申请号(应采集失败并记录)。
"""
from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.checksum import compute_check_digit, normalize  # noqa: E402
from mock_site.seed import build_patents  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "sample_cases.csv")


def messy_variants(num: str, idx: int) -> str:
    styles = [
        num,
        f"ZL{num}",
        f"{num[:4]}.{num[4]}.{num[5:12]}.{num[12]}",
        f"ZL {num[:4]} {num[4]} {num[5:12]} {num[12]}",
        "ｚｌ" + num,
        num.replace(num[6], str((int(num[6]) + 1) % 10), 1)  # 改一位 -> 校验必错
        if idx % 7 == 6 else num,
    ]
    return styles[idx % len(styles)]


def main() -> None:
    patents = list(build_patents().values())[:24]
    rows = [["申请号", "客户", "备注"]]
    for i, p in enumerate(patents):
        rows.append([messy_variants(p["appNo"], i),
                     p["applicant"], p["title"][:18]])

    # 缺校验位(可识别但不合法, 应拦截并给出推算值)
    rows.append([patents[0]["appNo"][:12], "华创科技有限公司", "缺校验位案例"])
    # 完全无法识别
    rows.append(["PATENT-NO-001", "恒远机械股份公司", "格式非法案例"])
    # 校验合法但 mock 库不存在
    base = "202120999999"
    rows.append([base + compute_check_digit(base), "云启软件有限公司",
                 "库里没有的案件"])
    # 旧式合法申请号(mock 库没有, 会走 404 失败路径)
    old_base = "85100123"
    rows.append([old_base + compute_check_digit(old_base),
                 "北辰新材料公司", "旧式申请号"])

    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)

    bad = [r for r in rows[1:] if not normalize(r[0]).ok]
    print(f"已写出 {OUT}: {len(rows)-1} 行, 其中校验/格式不过 {len(bad)} 行")
    for r in bad:
        print("  拦截:", r[0], "->", normalize(r[0]).reason)


if __name__ == "__main__":
    main()
