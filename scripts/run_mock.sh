#!/usr/bin/env bash
# 启动本地 mock 专利查询站 (默认 5051)
set -euo pipefail
cd "$(dirname "$0")/.."
# 测试/演示可调小: 窗口内超过 MOCK_RATE_MAX 次请求即封 MOCK_BAN_SECONDS 秒
export MOCK_RATE_WINDOW="${MOCK_RATE_WINDOW:-10}"
export MOCK_RATE_MAX="${MOCK_RATE_MAX:-5}"
export MOCK_BAN_SECONDS="${MOCK_BAN_SECONDS:-30}"
# 注: 不用 python -I, 因为 -I 会忽略 PYTHONPATH; 这是我们自己的应用代码
export PYTHONPATH="$PWD"
exec python3 -m mock_site.app
