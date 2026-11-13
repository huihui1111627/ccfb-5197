#!/usr/bin/env bash
# 启动监控 Web 控制台 (默认 5050)
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
exec python3 -m patent_monitor.webapp
