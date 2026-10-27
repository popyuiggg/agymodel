#!/bin/bash
# 启动 Antigravity 端点与速率检测台 (Clean Edition)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 清理可能残留的同端口进程
pkill -f "scratch/web_dashboard.py" 2>/dev/null || true
pkill -f "$SCRIPT_DIR/run.py" 2>/dev/null || true

exec python3 "$SCRIPT_DIR/run.py" "$@"
