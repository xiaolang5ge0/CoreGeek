"""P0 验收：任意畸形输入 → 合法空响应；服务链路端到端。"""
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import _bootstrap  # noqa: F401

from agent.brain import decide
from agent.server import Handler
from agent.telemetry import Telemetry

RESPONSE_KEYS = {"roleCommandMap", "prompt", "executeCmd"}


def assert_valid_response(case, response):
    case.assertIsInstance(response, dict)
    case.assertTrue(RESPONSE_KEYS.issubset(response.keys()))
    case.assertIsInstance(response["roleCommandMap"], dict)
    # 必须可被 JSON 序列化（判题器要解析）
    json.dumps(response, ensure_ascii=False)


class TestMalformedInputs(unittest.TestCase):
    def test_none(self):
        response, trace = decide(None)
        assert_valid_response(self, response)
        self.assertEqual(response["roleCommandMap"], {})

    def test_non_dict(self):
        for junk in ("garbage", [1, 2], 42, b"bytes"):
            response, _ = decide(junk)
            assert_valid_response(self, response)

    def test_empty_dict(self):
        response, trace = decide({})
        assert_valid_response(self, response)

    def test_garbage_round(self):
        response, trace = decide({"roundNo": "abc", "mapInfo": "???"})
        assert_valid_response(self, response)
        self.assertEqual(response["roleCommandMap"], {})

    def test_partial_fields(self):
        response, _ = decide({"roundNo": 1, "teamOur": {"roles": [{"id": 10010}]}})
        assert_valid_response(self, response)

    def test_real_fixture(self):
        payload = json.loads(_bootstrap.FIXTURE.read_text(encoding="utf-8"))
        response, trace = decide(payload)
        assert_valid_response(self, response)
        self.assertEqual(trace.get("day"), 1)
        self.assertFalse(trace.get("is_day"))


class TestServerEndToEnd(unittest.TestCase):
    def test_post_fixture(self):
        Handler.telemetry = Telemetry(enabled=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = _bootstrap.FIXTURE.read_bytes()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(RESPONSE_KEYS.issubset(body.keys()))
            # 畸形报文 → 合法空响应
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/", data=b"not-json", method="POST"
            )
            with urllib.request.urlopen(req2, timeout=5) as resp2:
                body2 = json.loads(resp2.read().decode("utf-8"))
            self.assertEqual(body2["roleCommandMap"], {})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
