"""
Antigravity 会话数据库 (SQLite) 解析核心。
适配 Antigravity v2.29+：
- 支持 WAL 实时修改时间检测 (.db-wal)
- 支持读取全局 conversation_summaries.db 获取官方会话标题与子智能体标识
- 支持解析多模态/带附件提问 (fn=19 -> sfn=2 & sfn=3) 及子智能体任务指令 (step_type=101)
- 同时解析 gen_metadata (端点、速率、Token 明细) 与 steps (提问/回答摘要)
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

SUMMARIES_DB_PATHS = [
    os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db"),
    os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db"),
]


def open_db_safe(db_path: str) -> Tuple[Optional[sqlite3.Connection], Optional[str]]:
    """
    安全打开 SQLite 数据库，三级防锁死策略：
    1. 标准只读连接 (PRAGMA query_only = ON)，自动合并读取同目录 .db-wal 最新数据
    2. URI immutable=1 无锁模式
    3. 临时文件快照模式 (连同 -wal / -shm 一并复制)
    """
    abs_path = os.path.abspath(db_path)
    if not os.path.exists(abs_path):
        return None, f"数据库文件不存在: {abs_path}"

    # 策略 1: 常规连接 + query_only (兼容 WAL 实时读取最新未 checkpoint 的数据)
    try:
        conn = sqlite3.connect(abs_path, timeout=3.0)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, None
    except Exception as e1:
        err1 = str(e1)

    # 策略 2: 临时快照读取 (优先于 immutable=1，因为 immutable=1 会忽略 .db-wal 中的新数据)
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
            elif os.path.exists(temp_base + ext):
                try:
                    os.remove(temp_base + ext)
                except OSError:
                    pass
        conn = sqlite3.connect(temp_base, timeout=2.0)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, None
    except Exception as e2:
        err2 = str(e2)

    # 策略 3: immutable=1 模式兜底
    try:
        conn = sqlite3.connect(f"file://{abs_path}?immutable=1", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, None
    except Exception as e3:
        return None, f"打开数据库失败: {err1} / {err2} / {e3}"


def load_summaries_index() -> Dict[str, Dict[str, Any]]:
    """
    从 Antigravity v2.29+ 新增的 conversation_summaries.db 中读取会话标题与元数据。
    返回 {conversation_id: {"title": ..., "agent_name": ..., "parent_id": ...}}
    """
    index: Dict[str, Dict[str, Any]] = {}
    for db_path in SUMMARIES_DB_PATHS:
        if not os.path.exists(db_path):
            continue
        conn, _ = open_db_safe(db_path)
        if not conn:
            continue
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT conversation_id, title, preview, agent_name, parent_conversation_id "
                "FROM conversation_summaries"
            )
            for cid, title, preview, agent_name, parent_id in cur.fetchall():
                if not cid:
                    continue
                clean_title = (title or "").strip()
                # 过滤掉异常策略报错文本作为标题的情况
                if clean_title.startswith("The prompt could not be submitted"):
                    clean_title = ""
                index[cid] = {
                    "title": clean_title,
                    "preview": (preview or "").strip(),
                    "agent_name": (agent_name or "").strip(),
                    "parent_id": (parent_id or "").strip(),
                }
        except Exception:
            pass
        finally:
            conn.close()
    return index


def clean_user_text(raw: str) -> str:
    """清理用户输入中的系统附加标签，提取纯净提问文本。"""
    if not raw:
        return ""
    text = raw.strip()
    m = re.search(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>", text)
    if m:
        text = m.group(1).strip()
    text = re.sub(r"<ADDITIONAL_METADATA>[\s\S]*?</ADDITIONAL_METADATA>", "", text)
    text = re.sub(r"<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>", "", text)
    return text.strip()


def _extract_user_text_from_fn19(val_bytes: bytes) -> str:
    """
    从 USER_INPUT (step_type=14) 的 fn=19 载荷中提取用户文本：
    - 优先读取 sfn=2 (纯文本提问)
    - 兼容读取 sfn=3 (带图片/文件引用时的多段内容结构)
    """
    sfn2_text = ""
    sfn3_parts: List[str] = []

    for sfn, swt, sval in parse_proto(val_bytes):
        if sfn == 2 and swt == "bytes" and isinstance(sval, bytes):
            cand = clean_user_text(decode_utf8(sval))
            if cand:
                sfn2_text = cand
                break
        elif sfn == 3 and swt == "bytes" and isinstance(sval, bytes):
            # sfn=3 内部 field 1 为文本段落，field 2 为文件/图片 URI
            sub = parse_proto(sval)
            extracted_sub = False
            for tf, tw, tv in sub:
                if tf == 1 and tw == "bytes" and isinstance(tv, bytes):
                    t_str = clean_user_text(decode_utf8(tv))
                    if t_str and not t_str.startswith("file://"):
                        sfn3_parts.append(t_str)
                        extracted_sub = True
            if not extracted_sub:
                raw_str = clean_user_text(decode_utf8(sval))
                # 去除头尾可能的控制字符
                raw_str = re.sub(r"^[\x00-\x1f]+", "", raw_str).strip()
                if raw_str and not raw_str.startswith("file://"):
                    sfn3_parts.append(raw_str)

    if sfn2_text:
        return sfn2_text
    return " ".join(sfn3_parts).strip()


def _extract_subagent_prompt_from_fn114(val_bytes: bytes) -> str:
    """从子智能体消息 (step_type=101, fn=114) 中提取任务描述。"""
    for sfn, swt, sval in parse_proto(val_bytes):
        if sfn == 1 and swt == "bytes" and isinstance(sval, bytes):
            txt = decode_utf8(sval)
            m = re.search(r"content=([\s\S]+)$", txt)
            return (m.group(1) if m else txt).strip()
    return ""


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

    info["ttft_seconds"] = f11_sec
    info["stream_seconds"] = f12_sec
    info["duration_seconds"] = round(f11_sec + f12_sec, 3)

    out_tok = info["output_tokens"]
    dur = info["duration_seconds"]
    if out_tok > 0 and dur > 0:
        info["tps"] = round(out_tok / dur, 2)
        if f12_sec >= 1.5:
            info["streaming_tps"] = round(out_tok / f12_sec, 2)
        else:
            info["streaming_tps"] = info["tps"]

    if not info["backend_model"]:
        m_back = re.search(rb"\x9a\x01[\x01-\x3f]([a-zA-Z0-9\.\-]+)", data)
        if m_back:
            info["backend_model"] = decode_utf8(m_back.group(1))

    return info


def _parse_tool_call_brief(raw_bytes: bytes) -> Optional[Dict[str, str]]:
    """解析 steps 中 Field 20 -> Field 7 的工具调用摘要。"""
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
    从 steps 表读取用户提问 (step_type=14 / 101) 与模型响应 (step_type=15)，
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

        cur.execute("SELECT idx, step_type, step_payload FROM steps WHERE step_type IN (14, 15, 101) ORDER BY idx ASC")
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
                    user_text = _extract_user_text_from_fn19(val)
                    if user_text:
                        break
            if user_text:
                user_steps.append({
                    "step_index": step_idx,
                    "timestamp": ts,
                    "time_str": time_str,
                    "text": user_text,
                })

        elif step_type == 101 and not user_steps:  # Subagent 初始输入指令
            for fn, wt, val in outer:
                if fn == 114 and wt == "bytes" and isinstance(val, bytes):
                    sub_text = _extract_subagent_prompt_from_fn114(val)
                    if sub_text:
                        user_steps.append({
                            "step_index": step_idx,
                            "timestamp": ts,
                            "time_str": time_str,
                            "text": f"[子智能体任务] {sub_text}",
                        })
                        break

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
    """快速提取会话第一条用户提问作为可读标题（兼容纯文本 sfn=2、多模态 sfn=3 与子智能体 step_type=101）。"""
    try:
        cur = conn.cursor()
        cur.execute("SELECT step_type, step_payload FROM steps WHERE step_type IN (14, 101) ORDER BY idx ASC LIMIT 3")
        for step_type, payload in cur.fetchall():
            if not payload:
                continue
            for fn, wt, val in parse_proto(payload):
                if step_type == 14 and fn == 19 and wt == "bytes" and isinstance(val, bytes):
                    txt = _extract_user_text_from_fn19(val)
                    if txt:
                        first_line = txt.splitlines()[0].strip()
                        return first_line[:64] + ("..." if len(first_line) > 64 else "")
                elif step_type == 101 and fn == 114 and wt == "bytes" and isinstance(val, bytes):
                    txt = _extract_subagent_prompt_from_fn114(val)
                    if txt:
                        first_line = txt.splitlines()[0].strip()
                        return f"[子智能体] {first_line[:56]}" + ("..." if len(first_line) > 56 else "")
    except Exception:
        pass
    return ""


def _get_effective_mtime(db_file: str) -> float:
    """
    获取会话数据库真实最后修改时间：
    兼顾主文件 .db 与 WAL 日志文件 .db-wal（当 .db-wal 非空时取两者最大值）。
    """
    mtime = os.path.getmtime(db_file)
    wal_file = db_file + "-wal"
    if os.path.exists(wal_file):
        try:
            if os.path.getsize(wal_file) > 0:
                wal_mtime = os.path.getmtime(wal_file)
                if wal_mtime > mtime:
                    mtime = wal_mtime
        except OSError:
            pass
    return mtime


def scan_all_conversations() -> List[Dict[str, Any]]:
    """扫描所有 Antigravity (App 与 CLI) 会话数据库并按实际修改时间降序排列。"""
    convs: List[Dict[str, Any]] = []
    for source, sdir in SEARCH_DIRS:
        if os.path.exists(sdir):
            for db_file in glob.glob(os.path.join(sdir, "*.db")):
                try:
                    mtime = _get_effective_mtime(db_file)
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


def list_conversations_summary(limit: int = 60) -> List[Dict[str, Any]]:
    """
    获取最近会话摘要列表。
    同时保证 App 与 CLI 来源均有覆盖，并结合 conversation_summaries.db 补全标题。
    """
    all_convs = scan_all_conversations()
    summaries_idx = load_summaries_index()

    # 选取前 limit 条，若其中没有 CLI 会话则额外补充最近 10 条 CLI 会话，确保前端切换 CLI 筛选时不为空
    selected = all_convs[:limit]
    selected_ids = {c["id"] for c in selected}
    cli_count = sum(1 for c in selected if c["source"] == "CLI")
    if cli_count < 10:
        for c in all_convs[limit:]:
            if c["source"] == "CLI" and c["id"] not in selected_ids:
                selected.append(c)
                selected_ids.add(c["id"])
                cli_count += 1
                if cli_count >= 10:
                    break

    results: List[Dict[str, Any]] = []

    for c in selected:
        cid = c["id"]
        turn_count = 0
        latest_endpoint = "未知"
        client_model = "-"
        model_enum = "-"
        latest_tps = 0.0
        title = ""

        s_meta = summaries_idx.get(cid) or {}
        agent_name = s_meta.get("agent_name") or ""

        conn, _ = open_db_safe(c["path"])
        if conn:
            try:
                title = extract_first_user_title(conn)
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM gen_metadata")
                turn_count = cur.fetchone()[0]
                if turn_count > 0:
                    # 取最近 5 条以防最后一条正在生成或中断导致字段为空
                    cur.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 5")
                    for (raw_data,) in cur.fetchall():
                        if not raw_data:
                            continue
                        meta = parse_gen_metadata_row(raw_data)
                        if client_model == "-" and meta.get("client_model"):
                            client_model = meta["client_model"]
                        if model_enum == "-" and meta.get("model_enum"):
                            model_enum = meta["model_enum"]
                        if latest_endpoint == "未知" and meta.get("backend_model"):
                            latest_endpoint = meta["backend_model"]
                            latest_tps = meta.get("tps") or 0.0
                        if latest_endpoint != "未知" and client_model != "-":
                            break
            except Exception:
                pass
            finally:
                conn.close()

        # 若 steps 中未提取到标题，回退使用 conversation_summaries.db 中的官方标题
        if not title and s_meta.get("title"):
            title = s_meta["title"]
        if not title and agent_name:
            title = f"[{agent_name}] 子智能体会话 {cid[:8]}"

        results.append({
            "id": cid,
            "source": c["source"],
            "path": c["path"],
            "mtime": c["mtime"],
            "updated_at": c["updated_at"],
            "title": title or f"会话 {cid[:8]}",
            "agent_name": agent_name,
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
        mtime = _get_effective_mtime(target)
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

        p_step = (planner_by_bot.get(bot_id) if bot_id else None) or planner_by_turn.get(idx)

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

    # 查找最近一条包含有效 backend_model 的轮次作为顶部状态展示
    latest_valid = turns[-1] if turns else {}
    for t in reversed(turns):
        if t.get("backend_model"):
            latest_valid = t
            break

    summaries_idx = load_summaries_index()
    s_meta = summaries_idx.get(selected["id"]) or {}
    first_title = (
        user_steps[0]["text"].splitlines()[0][:64]
        if user_steps
        else (s_meta.get("title") or f"会话 {selected['id'][:8]}")
    )

    return {
        "id": selected["id"],
        "source": selected["source"],
        "db_path": selected["path"],
        "mtime": selected["mtime"],
        "updated_at": selected["updated_at"],
        "title": first_title,
        "summary": {
            "total_turns": len(turns),
            "client_model": latest_valid.get("client_model") or "默认/未捕获",
            "backend_endpoint": latest_valid.get("backend_model") or "未知",
            "model_enum": latest_valid.get("model_enum") or "未知",
            "used_non_gemini": bool(latest_valid.get("used_non_gemini")),
            "unique_endpoints": unique_endpoints,
            "endpoint_consistent": len(unique_endpoints) <= 1,
            "latest_tps": latest_valid.get("tps") or 0.0,
            "latest_streaming_tps": latest_valid.get("streaming_tps") or 0.0,
            "avg_tps": avg_tps,
            "avg_streaming_tps": avg_stream_tps,
            "peak_tps": peak_tps,
            "latest_duration": latest_valid.get("duration_seconds") or 0.0,
            "total_duration": total_duration,
            "latest_ttft": latest_valid.get("ttft_seconds") or 0.0,
            "avg_ttft": avg_ttft,
            "total_prompt_tokens": total_prompt,
            "total_cached_tokens": total_cached,
            "total_output_tokens": total_output,
            "total_thinking_tokens": total_thinking,
            "total_content_tokens": total_content,
        },
        "turns": turns,
    }
