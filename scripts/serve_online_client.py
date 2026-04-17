#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Online 客户端本地服务：只起一个端口提供前端静态页；API 地址由 static/js/app.js 在 127.0.0.1 访问时默认指向远程 server，不在 URL 上追加 ?api=。
用法：在 lobster_online 根目录执行  python3 scripts/serve_online_client.py [端口] [API_BASE]
默认：端口 8000；第二参数仅用于控制台打印说明。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DEFAULT_API = "http://42.194.209.150"
DEFAULT_PORT = 8000


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    api_base = (sys.argv[2] if len(sys.argv) > 2 else DEFAULT_API).rstrip("/")
    if not (STATIC / "index.html").exists():
        print(f"[ERR] 未找到 {STATIC / 'index.html'}", file=sys.stderr)
        sys.exit(1)

    from http.server import HTTPServer, SimpleHTTPRequestHandler

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(ROOT), **k)

        def do_GET(self):
            raw = self.path
            path_part = raw.split("?", 1)[0]
            path_part = path_part.rstrip("/") or "/"
            if path_part == "/" or path_part == "/index.html":
                # 不设 ?api=：127.0.0.1 访问时 app.js 已默认 API_BASE=远程 server（见 static/js/app.js）
                self.path = "/static/index.html"
            return SimpleHTTPRequestHandler.do_GET(self)

        def list_directory(self, path):
            self.send_error(404, "Not Found")
            return None

        def log_message(self, format, *args):
            print(format % args)

    server = HTTPServer(("", port), Handler)
    print("================================================")
    print("  Lobster Online Client (port %s)" % port)
    print("  http://127.0.0.1:%s" % port)
    print("  API: %s" % api_base)
    print("================================================")
    print("  Ctrl+C 停止")
    server.serve_forever()


if __name__ == "__main__":
    main()
