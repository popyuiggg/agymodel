"""
Antigravity 会话数据库 (SQLite) 解析核心。
同时解析 gen_metadata (端点、速率、Token 明细) 与 steps (提问/回答摘要)，精准关联每一轮推理。
"""

import glob
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .proto import decode_utf8, parse_duration, parse_proto, parse_timestamp

SEARCH_DIRS = [
    ("App", os.path.expanduser("~/.gemini/antigravity/conversations")),
    ("CLI", os.path.expanduser("~/.gemini/antigravity-cli/conversations")),
]


def open_db_safe(db_path: str) -> Tuple[Optional[sqlite3.Connection], Optional[str]]:
    """
    安全打开 SQLite 数据库，三级防锁死策略：
    1. 标准只读连接 (PRAGMA query_only = ON)
    2. URI immutable=1 无锁模式
    3. 临时文件快照模式
    """
    abs_path = os.path.abspath(db_path)
    if not os.path.exists(abs_path):
        return None, f"数据库文件不存在: {abs_path}"

    # 策略 1: 常规连接 + query_only (兼容 WAL 实时读取)
    try:
        conn = sqlite3.connect(abs_path, timeout=3.0)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, None
    except Exception as e1:
        err1 = str(e1)

    # 策略 2: immutable=1 模式
    try:
        conn = sqlite3.connect(f"file://{abs_path}?immutable=1", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, None
    except Exception as e2:
        err2 = str(e2)

    # 策略 3: 临时快照读取
    try:
        temp_dir = tempfile.gettempdir()
        temp_base = os.path.join(temp_dir, f"agymodel_snap_{os.path.basename(abs_path)}")
        shutil.copy2(abs_path, temp_base)
        for ext in ("-wal", "-shm"):
            if os.path.exists(abs_path + ext):
                try:
                    shutil.copy2(abs_path + ext, temp_base + ext)
                except OSError:
                    pass
        conn = sqlite3.connect(temp_base)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, None
    except Exception as e3:
        return None, f"打开数据库失败: {err1} / {err2} / {e3}"


def clean_user_text(raw: str) -> str:
    """清理用户输入中的系统附加标签，提取纯净提问文本。"""
    if not raw:
        return ""
    text = raw.strip()
    m = re.search(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>", text)
    if m:
        text = m.group(1).strip()
    # 移除可能残留的元信息标签
    text = re.sub(r"<ADDITIONAL_METADATA>[\s\S]*?</ADDITIONAL_METADATA>", "", text)
    text = re.sub(r"<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>", "", text)
    return text.strip()


def parse_gen_metadata_row(data: bytes) -> Dict[str, Any]:
    """从单条 gen_metadata Protobuf 记录中提取完整端点与性能指标。"""
    info: Dict[str, Any] = {
        "client_model": None,
        "client_model_inherited": False,
        "backend_model": None,
        "model_enum": None,
        "model_id_num": None,
        "used_non_gemini": False,
        "used_claude": False,
        "request_id": None,
        "trajectory_id": None,
        "last_step_index": None,
        "bot_id": None,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "content_tokens": 0,
        "duration_seconds": 0.0,
        "ttft_seconds": 0.0,
        "stream_seconds": 0.0,
        "tps": 0.0,
        "streaming_tps": 0.0,
    }

    outer_fields = parse_proto(data)
    f11_sec = 0.0
    f12_sec = 0.0

    for fn, wt, val in outer_fields:
        # Field 3: 客户端请求配置 (通常保存在最新轮次中)
        if fn == 3 and wt == "bytes" and isinstance(val, bytes):
            for sfn, swt, sval in parse_proto(val):
                if sfn == 28 and swt == "bytes" and isinstance(sval, bytes):
                    cm = decode_utf8(sval)
                    if cm:
                        info["client_model"] = cm
            if not info["client_model"]:
                matches = re.findall(rb"(gemini-[\w\.-]+|claude-[\w\.-]+|gpt-[\w\.-]+)", val)
                for cand in matches:
                    cand_str = decode_utf8(cand)
                    if not cand_str.endswith(("-api", "-docs", "-dev", "-schema", "-guides", "-reference")):
                        info["client_model"] = cand_str
                        break

        # Field 1: 后端响应生成元数据
        elif fn == 1 and wt == "bytes" and isinstance(val, bytes):
            for sfn, swt, sval in parse_proto(val):
                # Field 19: 真实底层模型端点名称
                if sfn == 19 and swt == "bytes" and isinstance(sval, bytes):
                    bm = decode_utf8(sval)
                    if bm:
                        info["backend_model"] = bm

                # Field 20: 扩展键值对 Map
                elif sfn == 20 and swt == "bytes" and isinstance(sval, bytes):
                    k, v = None, None
                    for e_fn, e_wt, e_val in parse_proto(sval):
                        if e_fn == 1 and e_wt == "bytes" and isinstance(e_val, bytes):
                            k = decode_utf8(e_val)
                        elif e_fn == 2 and e_wt == "bytes" and isinstance(e_val, bytes):
                            v = decode_utf8(e_val)
                    if k == "model_enum":
                        info["model_enum"] = v
                    elif k == "request_id":
                        info["request_id"] = v
                    elif k == "trajectory_id":
                        info["trajectory_id"] = v
                    elif k == "last_step_index" and v is not None and v.isdigit():
                        info["last_step_index"] = int(v)
                    elif k == "used_non_gemini_model":
                        info["used_non_gemini"] = (v == "true")
                    elif k == "used_claude":
                        info["used_claude"] = (v == "true")

                # Field 4: Token 计数与 Bot ID
                elif sfn == 4 and swt == "bytes" and isinstance(sval, bytes):
                    for t_fn, t_wt, t_val in parse_proto(sval):
                        if t_fn == 1 and t_wt == "varint" and isinstance(t_val, int):
                            info["model_id_num"] = t_val
                        elif t_fn == 2 and t_wt == "varint" and isinstance(t_val, int):
                            info["prompt_tokens"] = t_val
                        elif t_fn == 3 and t_wt == "varint" and isinstance(t_val, int):
                            info["output_tokens"] = t_val
                        elif t_fn == 5 and t_wt == "varint" and isinstance(t_val, int):
                            info["cached_tokens"] = t_val
                        elif t_fn == 7 and t_wt == "bytes" and isinstance(t_val, bytes):
                            info["bot_id"] = decode_utf8(t_val)
                        elif t_fn == 9 and t_wt == "varint" and isinstance(t_val, int):
                            info["thinking_tokens"] = t_val
                        elif t_fn == 10 and t_wt == "varint" and isinstance(t_val, int):
                            info["content_tokens"] = t_val

                # Field 11: 首包等待耗时 TTFT (Duration)
                elif sfn == 11 and swt == "bytes" and isinstance(sval, bytes):
                    f11_sec = parse_duration(sval)

                # Field 12: 流式接收耗时 Stream Duration (Duration)
                elif sfn == 12 and swt == "bytes" and isinstance(sval, bytes):
                    f12_sec = parse_duration(sval)

    # Field 11 为 TTFT，Field 12 为流式传输耗时，两者之和为端到端总耗时
    info["ttft_seconds"] = f11_sec
    info["stream_seconds"] = f12_sec
    info["duration_seconds"] = round(f11_sec + f12_sec, 3)

    # 计算 TPS (端到端速率 & 流式解码速率)
    out_tok = info["output_tokens"]
    dur = info["duration_seconds"]
    if out_tok > 0 and dur > 0:
        info["tps"] = round(out_tok / dur, 2)
        if f12_sec >= 1.5:
            info["streaming_tps"] = round(out_tok / f12_sec, 2)
        else:
            info["streaming_tps"] = info["tps"]

    # 兜底正则提取 backend_model
    if not info["backend_model"]:
        m_back = re.search(rb"\x9a\x01[\x01-\x3f]([a-zA-Z0-9\.\-]+)", data)
        if m_back:
            info["backend_model"] = decode_utf8(m_back.group(1))

    return info


def _parse_tool_call_brief(raw_bytes: bytes) -> Optional[Dict[str, str]]:
    """解析 steps 中 Field 20 -> Field 7 的工具调用摘要 (只提取工具名与关键参数摘要)。"""
    name = ""
    args_raw = ""
    for fn, wt, val in parse_proto(raw_bytes):
        if fn == 2 and wt == "bytes" and isinstance(val, bytes):
            name = decode_utf8(val)
        elif fn == 3 and wt == "bytes" and isinstance(val, bytes):
            args_raw = decode_utf8(val)
    if not name:
        return None

    target = ""
    if args_raw:
        try:
            obj = json.loads(args_raw)
            if isinstance(obj, dict):
                target = (
                    obj.get("toolSummary")
                    or obj.get("CommandLine")
                    or obj.get("Path")
                    or obj.get("query")
                    or obj.get("Role")
                    or ""
                )
        except Exception:
            pass
    if len(target) > 72:
        target = target[:69] + "..."
    return {"name": name, "target": target}


def parse_steps_context(conn: sqlite3.Connection) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    从 steps 表读取用户提问 (step_type=14) 与模型响应 (step_type=15)，
    返回: (user_steps_list, planner_by_turn_idx, planner_by_bot_id)
    """
    user_steps: List[Dict[str, Any]] = []
    planner_by_turn: Dict[int, Dict[str, Any]] = {}
    planner_by_bot: Dict[str, Dict[str, Any]] = {}

    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='steps'")
        if not cur.fetchone():
            return user_steps, planner_by_turn, planner_by_bot

        cur.execute("SELECT idx, step_type, step_payload FROM steps WHERE step_type IN (14, 15) ORDER BY idx ASC")
        rows = cur.fetchall()
    except Exception:
        return user_steps, planner_by_turn, planner_by_bot

    for step_idx, step_type, payload in rows:
        if not payload:
            continue
        ts = 0.0
        turn_idx_from_meta: Optional[int] = None

        outer = parse_proto(payload)
        for fn, wt, val in outer:
            if fn == 5 and wt == "bytes" and isinstance(val, bytes):
                for sfn, swt, sval in parse_proto(val):
                    if sfn == 1 and swt == "bytes" and isinstance(sval, bytes):
                        ts = parse_timestamp(sval)
                    elif sfn == 20 and swt == "bytes" and isinstance(sval, bytes):
                        # Field 3 in sfn=20 stores gen_metadata turn index (0 is omitted in proto3 varint)
                        found_turn = 0
                        for mfn, mwt, mval in parse_proto(sval):
                            if mfn == 3 and mwt == "varint" and isinstance(mval, int):
                                found_turn = mval
                        turn_idx_from_meta = found_turn

        time_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts > 0 else ""

        if step_type == 14:  # USER_INPUT
            user_text = ""
            for fn, wt, val in outer:
                if fn == 19 and wt == "bytes" and isinstance(val, bytes):
                    for sfn, swt, sval in parse_proto(val):
                        if sfn == 2 and swt == "bytes" and isinstance(sval, bytes):
                            user_text = clean_user_text(decode_utf8(sval))
                            if user_text:
                                break
            if user_text:
                user_steps.append({
                    "step_index": step_idx,
                    "timestamp": ts,
                    "time_str": time_str,
                    "text": user_text,
                })

        elif step_type == 15:  # PLANNER_RESPONSE
            reply_text = ""
            thought_text = ""
            bot_id = ""
            tools: List[Dict[str, str]] = []

            for fn, wt, val in outer:
                if fn == 20 and wt == "bytes" and isinstance(val, bytes):
                    for sfn, swt, sval in parse_proto(val):
                        if sfn == 1 and swt == "bytes" and isinstance(sval, bytes) and not reply_text:
                            reply_text = decode_utf8(sval)
                        elif sfn == 3 and swt == "bytes" and isinstance(sval, bytes) and not thought_text:
                            thought_text = decode_utf8(sval)
                        elif sfn == 6 and swt == "bytes" and isinstance(sval, bytes):
                            bot_id = decode_utf8(sval)
                        elif sfn == 7 and swt == "bytes" and isinstance(sval, bytes):
                            tb = _parse_tool_call_brief(sval)
                            if tb:
                                tools.append(tb)

            p_info = {
                "step_index": step_idx,
                "timestamp": ts,
                "time_str": time_str,
                "turn_index": turn_idx_from_meta,
                "bot_id": bot_id,
                "reply_text": reply_text,
                "thought_text": thought_text,
                "tools": tools,
            }
            if turn_idx_from_meta is not None and turn_idx_from_meta not in planner_by_turn:
                planner_by_turn[turn_idx_from_meta] = p_info
            if bot_id:
                planner_by_bot[bot_id] = p_info

    return user_steps, planner_by_turn, planner_by_bot


def extract_first_user_title(conn: sqlite3.Connection) -> str:
    """快速提取会话第一条用户提问作为可读标题。"""
    try:
        cur = conn.cursor()
        cur.execute("SELECT step_payload FROM steps WHERE step_type = 14 ORDER BY idx ASC LIMIT 1")
        row = cur.fetchone()
        if not row or not row[0]:
            return ""
        for fn, wt, val in parse_proto(row[0]):
            if fn == 19 and wt == "bytes" and isinstance(val, bytes):
                for sfn, swt, sval in parse_proto(val):
                    if sfn == 2 and swt == "bytes" and isinstance(sval, bytes):
                        txt = clean_user_text(decode_utf8(sval))
                        if txt:
                            first_line = txt.splitlines()[0].strip()
                            return first_line[:64] + ("..." if len(first_line) > 64 else "")
    except Exception:
        pass
    return ""


def scan_all_conversations() -> List[Dict[str, Any]]:
    """扫描所有 Antigravity (App 与 CLI) 会话数据库并按修改时间降序排列。"""
    convs: List[Dict[str, Any]] = []
    for source, sdir in SEARCH_DIRS:
        if os.path.exists(sdir):
            for db_file in glob.glob(os.path.join(sdir, "*.db")):
                try:
                    mtime = os.path.getmtime(db_file)
                    cid = Path(db_file).stem
                    convs.append({
                        "id": cid,
                        "source": source,
                        "path": db_file,
                        "mtime": mtime,
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    })
                except OSError:
                    pass
    convs.sort(key=lambda x: x["mtime"], reverse=True)
    return convs


def list_conversations_summary(limit: int = 40) -> List[Dict[str, Any]]:
    """获取最近会话摘要列表（包含会话标题、轮次数、最新端点与速率）。"""
    convs = scan_all_conversations()[:limit]
    results: List[Dict[str, Any]] = []

    for c in convs:
        turn_count = 0
        latest_endpoint = "未知"
        client_model = "-"
        model_enum = "-"
        latest_tps = 0.0
        title = ""

        conn, _ = open_db_safe(c["path"])
        if conn:
            try:
                title = extract_first_user_title(conn)
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM gen_metadata")
                turn_count = cur.fetchone()[0]
                if turn_count > 0:
                    cur.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 1")
                    row = cur.fetchone()
                    if row and row[0]:
                        meta = parse_gen_metadata_row(row[0])
                        latest_endpoint = meta.get("backend_model") or "未知"
                        client_model = meta.get("client_model") or "-"
                        model_enum = meta.get("model_enum") or "-"
                        latest_tps = meta.get("tps") or 0.0
            except Exception:
                pass
            finally:
                conn.close()

        results.append({
            "id": c["id"],
            "source": c["source"],
            "path": c["path"],
            "mtime": c["mtime"],
            "updated_at": c["updated_at"],
            "title": title or f"会话 {c['id'][:8]}",
            "turn_count": turn_count,
            "backend_endpoint": latest_endpoint,
            "client_model": client_model,
            "model_enum": model_enum,
            "latest_tps": latest_tps,
        })

    return results


def inspect_conversation(target: Optional[str] = None, preferred_source: Optional[str] = None) -> Dict[str, Any]:
    """
    定位并完整解析指定会话（或最新活跃会话）的所有轮次端点数据与对话摘要。
    """
    convs = scan_all_conversations()
    selected: Optional[Dict[str, Any]] = None

    if target and os.path.exists(target):
        mtime = os.path.getmtime(target)
        selected = {
            "id": Path(target).stem,
            "source": preferred_source or "File",
            "path": os.path.abspath(target),
            "mtime": mtime,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
        }
    elif target:
        for c in convs:
            if c["id"] == target or c["id"].startswith(target):
                if preferred_source and c["source"].lower() != preferred_source.lower():
                    continue
                selected = c
                break
        if not selected:
            return {"error": f"未找到匹配 '{target}' 的会话数据库"}
    else:
        if preferred_source:
            filtered = [c for c in convs if c["source"].lower() == preferred_source.lower()]
            if filtered:
                selected = filtered[0]
        if not selected and convs:
            selected = convs[0]

    if not selected:
        return {"error": "未找到任何 Antigravity 会话数据库"}

    conn, err = open_db_safe(selected["path"])
    if not conn:
        return {"error": err or "无法打开会话数据库"}

    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gen_metadata'")
        if not cur.fetchone():
            conn.close()
            return {"error": "数据库中不存在 gen_metadata 表"}

        cur.execute("SELECT idx, data, size FROM gen_metadata ORDER BY idx ASC")
        raw_rows = cur.fetchall()

        user_steps, planner_by_turn, planner_by_bot = parse_steps_context(conn)
        conn.close()
    except Exception as e:
        conn.close()
        return {"error": f"查询数据库异常: {e}"}

    turns: List[Dict[str, Any]] = []
    for idx, data, size in raw_rows:
        t = parse_gen_metadata_row(data)
        t["turn_index"] = idx
        t["payload_size"] = size
        turns.append(t)

    # 回填被 Antigravity 裁剪的历史轮次 client_model
    # (Antigravity 仅在最新轮次保留 Field 3 请求配置，同会话同端点的历史轮次可继承显示)
    known_client_by_enum: Dict[str, str] = {}
    fallback_client: Optional[str] = None
    for t in reversed(turns):
        cm = t.get("client_model")
        if cm:
            if not fallback_client:
                fallback_client = cm
            key = t.get("model_enum") or t.get("backend_model") or ""
            if key and key not in known_client_by_enum:
                known_client_by_enum[key] = cm

    for t in turns:
        if not t.get("client_model"):
            key = t.get("model_enum") or t.get("backend_model") or ""
            inherited = known_client_by_enum.get(key) or fallback_client
            if inherited:
                t["client_model"] = inherited
                t["client_model_inherited"] = True

    # 关联每一轮对应的 User 提问与 Planner 响应摘要
    for t in turns:
        idx = t["turn_index"]
        bot_id = t.get("bot_id")
        last_step = t.get("last_step_index")

        # 优先通过 bot_id 精确匹配，其次通过 turn_index 匹配
        p_step = (planner_by_bot.get(bot_id) if bot_id else None) or planner_by_turn.get(idx)

        # 查找触发当前轮次的最近一条用户输入
        u_step = None
        is_direct_user_turn = False
        if user_steps:
            bound_step = last_step if last_step is not None else (p_step["step_index"] - 1 if p_step else 999999)
            for us in user_steps:
                if us["step_index"] <= bound_step:
                    u_step = us
                    is_direct_user_turn = (us["step_index"] == bound_step)
                else:
                    break
            if not u_step:
                u_step = user_steps[0]

        t["context"] = {
            "step_index": p_step["step_index"] if p_step else None,
            "time_str": p_step["time_str"] if p_step else "",
            "is_direct_user_turn": is_direct_user_turn,
            "user_step_index": u_step["step_index"] if u_step else None,
            "user_prompt": u_step["text"] if u_step else "",
            "reply_text": p_step["reply_text"] if p_step else "",
            "thought_text": p_step["thought_text"] if p_step else "",
            "tools": p_step["tools"] if p_step else [],
        }

    # 汇总统计指标
    total_prompt = sum(t["prompt_tokens"] for t in turns)
    total_cached = sum(t["cached_tokens"] for t in turns)
    total_output = sum(t["output_tokens"] for t in turns)
    total_thinking = sum(t["thinking_tokens"] for t in turns)
    total_content = sum(t["content_tokens"] for t in turns)

    tps_list = [t["tps"] for t in turns if t["tps"] > 0]
    stream_tps_list = [t["streaming_tps"] for t in turns if t["streaming_tps"] > 0]
    ttft_list = [t["ttft_seconds"] for t in turns if t["ttft_seconds"] > 0]
    dur_list = [t["duration_seconds"] for t in turns if t["duration_seconds"] > 0]

    avg_tps = round(sum(tps_list) / len(tps_list), 2) if tps_list else 0.0
    peak_tps = round(max(tps_list), 2) if tps_list else 0.0
    avg_stream_tps = round(sum(stream_tps_list) / len(stream_tps_list), 2) if stream_tps_list else 0.0
    avg_ttft = round(sum(ttft_list) / len(ttft_list), 3) if ttft_list else 0.0
    total_duration = round(sum(dur_list), 2)

    unique_endpoints = []
    for t in turns:
        ep = t.get("backend_model")
        if ep and ep not in unique_endpoints:
            unique_endpoints.append(ep)

    latest = turns[-1] if turns else {}
    first_title = user_steps[0]["text"].splitlines()[0][:64] if user_steps else f"会话 {selected['id'][:8]}"

    return {
        "id": selected["id"],
        "source": selected["source"],
        "db_path": selected["path"],
        "mtime": selected["mtime"],
        "updated_at": selected["updated_at"],
        "title": first_title,
        "summary": {
            "total_turns": len(turns),
            "client_model": latest.get("client_model") or "默认/未捕获",
            "backend_endpoint": latest.get("backend_model") or "未知",
            "model_enum": latest.get("model_enum") or "未知",
            "used_non_gemini": bool(latest.get("used_non_gemini")),
            "unique_endpoints": unique_endpoints,
            "endpoint_consistent": len(unique_endpoints) <= 1,
            "latest_tps": latest.get("tps") or 0.0,
            "latest_streaming_tps": latest.get("streaming_tps") or 0.0,
            "avg_tps": avg_tps,
            "avg_streaming_tps": avg_stream_tps,
            "peak_tps": peak_tps,
            "latest_duration": latest.get("duration_seconds") or 0.0,
            "total_duration": total_duration,
            "latest_ttft": latest.get("ttft_seconds") or 0.0,
            "avg_ttft": avg_ttft,
            "total_prompt_tokens": total_prompt,
            "total_cached_tokens": total_cached,
            "total_output_tokens": total_output,
            "total_thinking_tokens": total_thinking,
            "total_content_tokens": total_content,
        },
        "turns": turns,
    }
