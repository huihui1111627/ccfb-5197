#!/usr/bin/env bash
# 演示前置: 生成 sample_cases.csv, 并提示后续启动步骤
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
python3 scripts/seed_demo.py
echo
echo "接下来:"
echo "  1) 终端 A: bash scripts/run_mock.sh     # mock 查询站 :5051"
echo "  2) 终端 B: bash scripts/run_web.sh      # 控制台     :5050"
echo "  3) 控制台 -> 案件清单 -> 导入 data/sample_cases.csv -> 采集任务 -> 立即触发"
