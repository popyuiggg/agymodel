#!/bin/bash
# 启动 Antigravity 端点与速率检测台 (Clean Edition)
# 默认以后台守护进程运行 HTTP 服务并弹出独立桌面窗口，关闭终端不会中断服务。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8765
URL="http://localhost:$PORT"

# 若带有命令行参数 (如 -l, -a, --cli 等)，直接透传执行
if [ $# -gt 0 ]; then
    exec python3 "$SCRIPT_DIR/run.py" "$@"
fi

# 清理旧版进程並重启最新后台服务
pkill -f "scratch/web_dashboard.py" 2>/dev/null || true
pkill -f "$SCRIPT_DIR/run.py" 2>/dev/null || true
sleep 0.2

nohup python3 "$SCRIPT_DIR/run.py" -p "$PORT" --no-open >/tmp/agymodel.log 2>&1 &
PID=$!

# 等待端口就绪后再打开窗口，避免首屏请求失败
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if lsof -i ":$PORT" >/dev/null 2>&1; then
        break
    fi
    sleep 0.15
done

python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); from agymodel.server import open_ui_window; open_ui_window('$URL', 'app')"
echo "✅ Antigravity 端点检测台已在后台启动: $URL (PID: $PID)"
