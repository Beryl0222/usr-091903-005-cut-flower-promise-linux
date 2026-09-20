"""鲜切花保鲜承诺的运行入口。

- GET  /health                 服务身份（保持既有契约）
- 任意 /api/*                  保鲜承诺领域 JSON API
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from freshness.api import APIHandler, build_service, dispatch

SERVICE_ID = "cut-flower-promise"
SERVICE_NAME = "鲜切花保鲜承诺"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def make_handler(data_path=None):
    service = build_service(data_path)
    api = APIHandler(service)

    class Handler(BaseHTTPRequestHandler):
        """健康检查与 /api 下的领域接口。"""

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._write_json(200, health_payload())
                return
            if parsed.path.startswith("/api/"):
                status, payload = dispatch(
                    api, "GET", parsed.path[4:], parse_qs(parsed.query), {}
                )
                self._write_json(status, payload)
                return
            self.send_error(404)

        def do_POST(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._write_json(400, {"error": "bad_json"})
                return
            status, payload = dispatch(api, "POST", parsed.path[4:], parse_qs(parsed.query), body)
            self._write_json(status, payload)

        def _write_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler


# 默认（纯内存）处理器，便于本地联调与既有契约测试引用
Handler = make_handler()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=None, help="事件流 JSONL 文件路径（缺省为纯内存）")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 领域装配自检：能从空事件流构建服务
        build_service(args.data)
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(args.data)).serve_forever()


if __name__ == "__main__":
    main()
