#!/usr/bin/env python3
"""
Antigravity 端点与性能检测工具 (agymodel) 统一入口。

用法示例:
  1. 启动独立桌面窗口模式 (默认):
     python3 run.py
  2. 启动并在默认浏览器标签页打开:
     python3 run.py --browser
  3. 仅启动本地后台 HTTP 服务:
     python3 run.py --no-open
  4. 命令行终端检测模式 (CLI):
     python3 run.py --cli              # 查看最新会话最新一轮
     python3 run.py --cli -a           # 查看最新会话全部轮次表格
     python3 run.py --cli -l           # 列出最近会话列表
     python3 run.py --cli -c 9465 -a   # 查看指定会话全部轮次
"""

import argparse
import sys

from agymodel.cli import run_cli
from agymodel.server import DEFAULT_PORT, start_server


def main() -> None:
    # 若包含 CLI 专属参数 (--cli, -l, --list, -c, --conv, -a, --all, --json)，直接分流至命令行模式
    cli_flags = {"--cli", "-l", "--list", "-c", "--conv", "--conversation-id", "-a", "--all", "--json", "-d", "--db"}
    if any(arg in cli_flags for arg in sys.argv[1:]):
        filtered_args = [a for a in sys.argv[1:] if a != "--cli"]
        run_cli(filtered_args)
        return

    parser = argparse.ArgumentParser(description="Antigravity 端点与性能检测工具")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"服务监听端口 (默认 {DEFAULT_PORT})")
    parser.add_argument("--app", action="store_true", help="使用无边框独立桌面窗口打开 (默认)")
    parser.add_argument("--browser", action="store_true", help="使用系统默认浏览器标签页打开")
    parser.add_argument("--no-open", action="store_true", help="仅启动服务，不自动打开窗口")
    parser.add_argument("--cli", action="store_true", help="使用命令行终端检测模式")

    args = parser.parse_args()
    open_mode = "none" if args.no_open else ("browser" if args.browser else "app")
    start_server(port=args.port, open_mode=open_mode)


if __name__ == "__main__":
    main()
