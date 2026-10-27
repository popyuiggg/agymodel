"""
Antigravity 端点检测工具 - 命令行界面 (CLI)。
兼容原 check_model_endpoint.py 与 agy_model_check.py 的全部参数习惯。
"""

import argparse
import json
import sys
from typing import Optional

from .parser import inspect_conversation, list_conversations_summary


def print_conversation_list(limit: int = 15) -> None:
    items = list_conversations_summary(limit=limit)
    if not items:
        print("未发现任何 Antigravity 会话数据库 (~/.gemini/antigravity/)。")
        return

    print("\n" + "─" * 92)
    print(f" {'来源':<5} {'最后更新':<19} {'会话ID前缀':<10} {'轮次':>4}  {'后端真实端点':<22} {'TPS':>7}  {'会话标题'}")
    print("─" * 92)
    for c in items:
        tps_str = f"{c['latest_tps']:.1f}" if c["latest_tps"] else "-"
        title = (c["title"] or "")[:24]
        print(
            f" [{c['source']:<3}] {c['updated_at']:<19} {c['id'][:8]:<10} "
            f"{c['turn_count']:>4}  \033[1;32m{c['backend_endpoint']:<22}\033[0m {tps_str:>7}  {title}"
        )
    print("─" * 92)
    print("提示: 使用 `-c <ID前缀>` 查看指定会话，加上 `-a` 查看所有轮次表格。\n")


def run_cli(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="Antigravity 模型端点与 TPS 检测工具 (CLI)")
    parser.add_argument("-c", "--conv", "--conversation-id", dest="conv", help="指定会话 ID 或前缀 (如 -c 9465)")
    parser.add_argument("-d", "--db", help="指定数据库完整路径 (.db)")
    parser.add_argument("-l", "--list", action="store_true", help="列出最近会话列表")
    parser.add_argument("-a", "--all", action="store_true", help="展示该会话的全部推理轮次")
    parser.add_argument("--app", action="store_true", help="仅筛选桌面 App 会话")
    parser.add_argument("--cli-source", action="store_true", help="仅筛选 CLI 会话")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式结果")

    args = parser.parse_args(argv)

    if args.list:
        print_conversation_list()
        return

    target = args.db or args.conv
    pref = "App" if args.app else ("CLI" if args.cli_source else None)
    data = inspect_conversation(target=target, preferred_source=pref)

    if "error" in data:
        print(f"[错误] {data['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        if not args.all and data.get("turns"):
            data = dict(data)
            data["turns"] = [data["turns"][-1]]
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    s = data["summary"]
    turns = data["turns"]
    display_turns = turns if args.all else (turns[-1:] if turns else [])

    print("\n" + "═" * 86)
    print(f" Antigravity 端点与性能检测  [{data['source']}]  {data['title']}")
    print("═" * 86)
    print(f" 会话 ID:      {data['id']}   (更新于 {data['updated_at']})")
    print(f" 配置模型:     {s['client_model']}")
    print(f" 真实后端端点: \033[1;32m{s['backend_endpoint']}\033[0m   (内部枚举: {s['model_enum']})")
    print(
        f" 速率与耗时:   最新 \033[1;33m{s['latest_tps']} tok/s\033[0m | "
        f"平均 {s['avg_tps']} tok/s | 峰值 {s['peak_tps']} tok/s | 平均首字(TTFT) {s['avg_ttft']}s"
    )
    print(
        f" 累计 Token:   新增输入 {s['total_prompt_tokens']:,} | 缓存命中 {s['total_cached_tokens']:,} | "
        f"输出 {s['total_output_tokens']:,} (思考 {s['total_thinking_tokens']:,} / 正文 {s['total_content_tokens']:,})"
    )
    print("─" * 86)

    if not display_turns:
        print(" 暂无推理记录 (gen_metadata 为空)")
        print("═" * 86 + "\n")
        return

    print(
        f" {'轮次':<5} {'时间':<9} {'客户端配置':<21} {'后端真实端点':<20} "
        f"{'TPS':>7} {'耗时(TTFT)':>14} {'输入(缓存)':>14} {'输出(思/文)':>14}"
    )
    print("─" * 86)
    for t in display_turns:
        ctx = t.get("context") or {}
        t_str = ctx.get("time_str") or "-"
        cm = (t.get("client_model") or "-")[:20]
        bm = (t.get("backend_model") or "未知")[:19]
        tps_s = f"{t['tps']:.1f}"
        dur_s = f"{t['duration_seconds']:.1f}s({t['ttft_seconds']:.1f}s)"
        in_s = f"{t['prompt_tokens'] // 1000}k({t['cached_tokens'] // 1000}k)" if t["cached_tokens"] >= 1000 else f"{t['prompt_tokens']}({t['cached_tokens']})"
        out_s = f"{t['output_tokens']}({t['thinking_tokens']}/{t['content_tokens']})"
        print(
            f" #{t['turn_index']:<4} {t_str:<9} {cm:<21} \033[1;32m{bm:<20}\033[0m "
            f"\033[1;33m{tps_s:>7}\033[0m {dur_s:>14} {in_s:>14} {out_s:>14}"
        )
    print("═" * 86 + "\n")
