"""T7-T8: HTTP request validation (via handler internals) and limits."""

import io
import json
import unittest

from docrot_scan_api import server as server_mod
from tests.helpers import make_config


class FakeSocket:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO()

    def makefile(self, *a, **k):
        return self


def run_post(handler_target, config, body: bytes, content_type="application/json",
             path="/v1/scan", method="POST"):
    """Drive DocrotHandler._handle_scan-ish flow with a fake request line."""

    captured = {}

    class TestableHandler(server_mod.DocrotHandler):
        def __init__(self):
            self.headers = {"Content-Type": content_type,
                            "Content-Length": str(len(body))}
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.command = method
            self.path = path
            self.client_address = ("127.0.0.1", 12345)
            self.request_version = "HTTP/1.1"
            self.close_connection = False
            self.config = config
            self.service = None
            self.access_logger = None
            self.wellknown_body = {"x": 1}

        def send_response(self, code, *a):
            captured.setdefault("status", code)

        def send_header(self, name, value):
            captured.setdefault("headers", {})[name] = value

        def end_headers(self):
            pass

        def _send_json(self, status, payload, request_id=None):
            captured["status"] = status
            captured["payload"] = payload

    h = TestableHandler()
    status = h._handle_scan("req-test-1")
    return status, captured.get("payload", {}), h


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_malformed_json(self):
        status, payload, _ = run_post(None, self.config, b"{nope")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

    def test_non_object_json(self):
        status, payload, _ = run_post(None, self.config, b"[1,2]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

    def test_missing_repository(self):
        status, payload, _ = run_post(None, self.config, b'{"ref":"main"}')
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "missing_field")

    def test_unknown_field(self):
        status, payload, _ = run_post(
            None, self.config, b'{"repository":"https://github.com/o/r","evil":1}')
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "unknown_field")

    def test_ref_wrong_type(self):
        status, payload, _ = run_post(
            None, self.config, b'{"repository":"https://github.com/o/r","ref":7}')
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_field")

    def test_bad_content_type(self):
        status, payload, _ = run_post(None, self.config, b"{}",
                                      content_type="text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

    def test_body_too_large(self):
        cfg = make_config(DOCROT_MAX_REQUEST_BYTES="1024")
        big = b'{"repository":"' + b"a" * 5000 + b'"}'
        status, payload, handler = run_post(None, cfg, big)
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "request_too_large")
        self.assertTrue(handler.close_connection)

    def test_wrong_repository_host(self):
        status, payload, _ = run_post(
            None, self.config,
            json.dumps({"repository": "https://gitlab.com/o/r"}).encode())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "unsupported_repository_host")

    def test_wrong_scheme(self):
        status, payload, _ = run_post(
            None, self.config,
            json.dumps({"repository": "http://github.com/o/r"}).encode())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "unsupported_repository_scheme")

    def test_invalid_ref(self):
        status, payload, _ = run_post(
            None, self.config,
            json.dumps({"repository": "https://github.com/o/r",
                        "ref": "--upload-pack=x"}).encode())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "invalid_ref")

    def test_unknown_route_404(self):
        class TestableHandler(server_mod.DocrotHandler):
            def __init__(self):
                self.headers = {}
                self.wfile = io.BytesIO()
                self.command = "GET"
                self.path = "/nope"
                self.client_address = ("127.0.0.1", 1)
                self.config = self.config = make_config()
                self.access_logger = None
                self.wellknown_body = None

            def send_response(self, code, *a):
                self.status = code

            def send_header(self, *a):
                pass

            def end_headers(self):
                pass

        h = TestableHandler()
        h.do_GET()
        self.assertEqual(h.status, 404)


if __name__ == "__main__":
    unittest.main()
