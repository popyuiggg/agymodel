"""
Antigravity 端点检测工具 - 本地 HTTP 服务与独立窗口启动器。
采用 ThreadingHTTPServer 与前后端分离静态资源目录。
"""

import json
import mimetypes
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .parser import inspect_conversation, list_conversations_summary

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DEFAULT_PORT = 8765

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


class AgyRequestHandler(BaseHTTPRequestHandler):
    """轻量级 REST API 与静态文件请求处理器。"""

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. API: 会话列表
        if path == "/api/conversations":
            limit_str = query.get("limit", ["60"])[0]
            limit = int(limit_str) if limit_str.isdigit() else 60
            self._send_json(list_conversations_summary(limit=limit))
            return

        # 2. API: 单会话详情与轮次表
        if path == "/api/conversation":
            cid = query.get("id", [""])[0] or None
            source = query.get("source", [""])[0] or None
            data = inspect_conversation(target=cid, preferred_source=source)
            self._send_json(data)
            return

        # 3. 静态资源服务 (index.html, style.css, app.js)
        if path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return

        rel_path = path.lstrip("/")
        target_file = (STATIC_DIR / rel_path).resolve()
        if str(target_file).startswith(str(STATIC_DIR.resolve())) and target_file.is_file():
            mime, _ = mimetypes.guess_type(str(target_file))
            content_type = (
                f"{mime or 'application/octet-stream'}; charset=utf-8"
                if mime and mime.startswith(("text/", "application/javascript", "application/json"))
                else (mime or "application/octet-stream")
            )
            self._serve_file(target_file, content_type)
            return

        self.send_response(404)
        self.end_headers()

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        try:
            content = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def _send_json(self, data: Any) -> None:
        try:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: Any) -> None:
        pass


def open_ui_window(url: str, mode: str = "app") -> None:
    """
    打开前端界面：
    - 使用 localhost 配合 --proxy-bypass-list=<-loopback> 绕过系统代理拦截
    """
    if mode == "app" and sys.platform == "darwin":
        for bin_path in CHROME_CANDIDATES:
            if os.path.exists(bin_path):
                try:
                    subprocess.Popen(
                        [
                            bin_path,
                            f"--app={url}",
                            "--window-size=1280,820",
                            "--proxy-bypass-list=<-loopback>",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
                except Exception:
                    pass

    if sys.platform == "darwin":
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform == "win32":
        os.startfile(url)  # type: ignore


def start_server(port: int = DEFAULT_PORT, open_mode: str = "app") -> None:
    """启动本地 HTTP 服务并根据 open_mode 唤起窗口 ('app' | 'browser' | 'none')。"""
    bound_port = None
    server = None

    for p in range(port, port + 15):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), AgyRequestHandler)
            bound_port = p
            break
        except OSError as e:
            if e.errno in (48, 98):  # Address already in use
                continue
            raise

    if not server or not bound_port:
        print(f"[错误] 端口 {port}~{port + 14} 均被占用，请释放端口后重试。", file=sys.stderr)
        sys.exit(1)

    # 使用 localhost 以匹配 macOS 系统代理默认 ExceptionsList，避免本地代理拦截 127.0.0.1
    url = f"http://localhost:{bound_port}"
    print("─" * 62)
    print(" ✨ Antigravity 端点检测工具 (Clean Edition) 已启动")
    print("─" * 62)
    print(f" 访问地址:  \033[1;32m{url}\033[0m")
    print(
        f" 窗口模式:  "
        f"{'独立桌面窗口 (App Mode)' if open_mode == 'app' else ('默认浏览器' if open_mode == 'browser' else '仅后台服务')}"
    )
    print(" 退出服务:  按 Ctrl + C")
    print("─" * 62)
    sys.stdout.flush()

    if open_mode in ("app", "browser"):
        threading.Timer(0.25, open_ui_window, args=(url, open_mode)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止 Antigravity 端点检测服务。")
        server.server_close()
