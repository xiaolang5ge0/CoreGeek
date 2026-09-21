"""L0 接入层：HTTP 服务 + Exception Fallback。只搬数据，零游戏逻辑。

红线：5 秒响应、5 次异常判负 → 任何内部异常都回落为合法空响应。
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .brain import decide
from .telemetry import Telemetry

LOGGER = logging.getLogger(__name__)

FALLBACK_BODY = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'


class Handler(BaseHTTPRequestHandler):
    telemetry: Telemetry | None = None  # 由 serve() 注入

    def do_POST(self) -> None:
        body = FALLBACK_BODY
        payload: Any = None
        response: Any = None
        trace: dict[str, Any] = {"fatal": "request_exception"}
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            response, trace = decide(payload)
            try:
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            except Exception:
                LOGGER.exception("response serialize failed")
                body = FALLBACK_BODY
        except Exception:
            LOGGER.exception("request handling failed")
            body = FALLBACK_BODY
        self._log(payload, response, trace)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            LOGGER.exception("response write failed")

    def _log(self, payload: Any, response: Any, trace: dict[str, Any]) -> None:
        try:
            if self.telemetry is not None:
                round_no = payload.get("roundNo") if isinstance(payload, dict) else None
                self.telemetry.log_round(round_no, payload, response, trace)
        except Exception:
            pass

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(port: int) -> None:
    Handler.telemetry = Telemetry()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
