"""T20-T22: local integration test.

Starts the REAL DocrotHandler server on an ephemeral loopback port and drives
it over HTTP. The only thing mocked is the outbound GitHub archive fetch
(ScanService._fetcher) plus DNS+outbound URL checking, so no external network
is touched.
"""

import http.client
import io
import json
import unittest
from unittest import mock

from docrot_scan_api import service as service_mod
from docrot_scan_api.jsonl import JsonlLogger
from docrot_scan_api.server import make_server
from docrot_scan_api.service import ScanService
from tests.helpers import jsonl_lines, make_config, simple_repo_tar, tar_bytes

REPO_URL = "https://github.com/owner/repo"


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.access_stream = io.StringIO()
        cls.job_stream = io.StringIO()
        cls.config = make_config(DOCROT_PORT="0")
        cls.config.host = "127.0.0.1"
        cls.config.port = 0

        # Archive the fake "GitHub" will serve: two broken urls, one ok.
        cls.archive = tar_bytes([
            ("repo/README.md", "f",
             "# Docs\n[dead](https://bad.test/gone)\n"
             "[alive](https://good.test/page)\n"
             "<img src='https://bad.test/img-404.png'>\n"),
            ("repo/docs/guide.rst", "f",
             "`guide <https://good.test/rst>`_\n"
             "rotten https://bad.test/bare-500\n"),
            ("repo/index.html", "f",
             '<a href="https://good.test/html">x</a>\n'),
            ("repo/code.py", "f", "ignored = 1\n"),
        ])

        # URL check outcomes (stands in for the outbound HTTP checker)
        class StubChecker:
            def __init__(self):
                self.last = None

            def __call__(self, urls, **kwargs):
                from docrot_scan_api.checker import CheckOutcome
                self.last = {"urls": urls, "kwargs": kwargs}
                outcomes = {
                    "https://bad.test/gone": (404, "HTTP 404"),
                    "https://bad.test/img-404.png": (404, "HTTP 404"),
                    "https://bad.test/bare-500": (500, "HTTP 500"),
                    "https://good.test/page": (200, None),
                    "https://good.test/rst": (200, None),
                    "https://good.test/html": (200, None),
                }
                return {u: CheckOutcome(status=s, error=e)
                        for u, (s, e) in outcomes.items()}

        cls.stub_checker = StubChecker()

        cls.service = ScanService(cls.config,
                                  job_logger=JsonlLogger("job",
                                                         stream=cls.job_stream),
                                  checker=cls.stub_checker)

        def fake_fetch(owner, repo, ref):
            return cls.archive

        cls.service._fetcher = fake_fetch

        cls.access_logger = JsonlLogger("access", stream=cls.access_stream)
        cls.wellknown = {
            "name": "docrot-scan-api",
            "pricing": {"amountUsd": 1.0, "currency": "SOL",
                        "payTo": "CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn",
                        "automatic": False},
        }

        cls.httpd = make_server(cls.config, cls.service,
                                access_logger=cls.access_logger,
                                wellknown_body=cls.wellknown)
        cls.port = cls.httpd.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      kwargs={"poll_interval": 0.05},
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    # ------------------------------------------------------------- helpers

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    # --------------------------------------------------------------- tests

    def test_health(self):
        status, headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertIn("X-Request-Id", headers)

    def test_root_describes_service(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["service"], "docrot-scan-api")
        self.assertIn("/v1/scan", doc["endpoints"]["scan"])
        self.assertEqual(doc["pricing"]["billingModel"],
                         "manual-invoicing-pilot")
        self.assertEqual(doc["pricing"]["payTo"],
                         "CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn")
        # v1.4.1: a relative docs pointer 404s behind the edge — must be absolute
        self.assertEqual(doc["docs"],
                         "https://github.com/auroraxo/docrot-api/blob/main/docs/API.md")

    def test_wellknown_descriptor(self):
        status, _, body = self.request("GET", "/.well-known/agent-service.json")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["name"], "docrot-scan-api")
        self.assertFalse(doc["pricing"]["automatic"])

    def test_unknown_route_404(self):
        status, _, body = self.request("GET", "/definitely-not-here")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_scan_wrong_method(self):
        status, _, body = self.request("GET", "/v1/scan")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "method_not_allowed")

    def test_scan_end_to_end(self):
        payload = json.dumps({"repository": REPO_URL, "ref": "main"})
        status, headers, body = self.request(
            "POST", "/v1/scan", body=payload,
            headers={"Content-Type": "application/json"})

        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result["repository"], REPO_URL)
        self.assertEqual(result["ref"], "main")
        self.assertEqual(result["scannedFiles"], 3)
        self.assertEqual(result["checkedUrls"], 6)
        self.assertEqual(result["requestId"], headers["X-Request-Id"])

        broken = result["broken"]
        self.assertEqual(len(broken), 3)
        by_url = {}
        for b in broken:
            self.assertEqual(
                set(b), {"url", "status", "source", "line", "error"})
            by_url.setdefault(b["url"], []).append(b)
        self.assertEqual(set(by_url), {
            "https://bad.test/gone",
            "https://bad.test/img-404.png",
            "https://bad.test/bare-500",
        })
        gone = by_url["https://bad.test/gone"][0]
        self.assertEqual((gone["status"], gone["source"], gone["line"]),
                         (404, "README.md", 2))
        img = by_url["https://bad.test/img-404.png"][0]
        self.assertEqual((img["source"], img["line"]), ("README.md", 4))
        bare = by_url["https://bad.test/bare-500"][0]
        self.assertEqual((bare["source"], bare["line"]),
                         ("docs/guide.rst", 2))
        self.assertIn("durationMs", result)
        self.assertIsInstance(result["durationMs"], int)

        receipt = result["receipt"]
        self.assertEqual(receipt["kind"], "scan-completed")
        self.assertEqual(receipt["billing"]["amountDueUsd"], 1.0)
        self.assertEqual(receipt["billing"]["payTo"],
                         "CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn")
        self.assertEqual(receipt["billing"]["model"],
                         "manual-invoicing-pilot")
        self.assertIn("manual", receipt["billing"]["terms"])

        # the checker received exactly the distinct urls, bounded kwargs
        self.assertEqual(sorted(self.stub_checker.last["urls"]), sorted({
            "https://bad.test/gone", "https://good.test/page",
            "https://bad.test/img-404.png", "https://bad.test/bare-500",
            "https://good.test/rst", "https://good.test/html"}))
        self.assertEqual(self.stub_checker.last["kwargs"]["concurrency"],
                         self.config.check_concurrency)

    def test_scan_validation_error_over_http(self):
        status, _, body = self.request(
            "POST", "/v1/scan", body=json.dumps({"repository":
                                                 "https://gitlab.com/o/r"}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "unsupported_repository_host")

    def test_scan_invalid_json_over_http(self):
        status, _, body = self.request(
            "POST", "/v1/scan", body="{oops",
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_json")

    def test_scan_oversize_body_over_http(self):
        cfg_limit = self.config.max_request_bytes
        big = json.dumps({"repository": REPO_URL,
                          "ref": "a" * (cfg_limit + 10)})
        status, _, body = self.request(
            "POST", "/v1/scan", body=big,
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "request_too_large")

    def test_scan_job_error_over_http(self):
        payload = json.dumps({"repository": "https://github.com/missing/repo"})
        svc = self.service

        from docrot_scan_api.extract import FetchError

        def failing_fetch(owner, repo, ref):
            raise FetchError("repository_or_ref_not_found",
                             "upstream returned HTTP 404", 404)

        original = svc._fetcher
        svc._fetcher = failing_fetch
        try:
            status, _, body = self.request(
                "POST", "/v1/scan", body=payload,
                headers={"Content-Type": "application/json"})
        finally:
            svc._fetcher = original
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "repository_or_ref_not_found")

    def test_scan_no_docs_over_http(self):
        svc = self.service
        svc._fetcher = lambda *a: tar_bytes(
            [("repo/code.py", "f", "x = 1\n")])
        try:
            status, _, body = self.request(
                "POST", "/v1/scan",
                body=json.dumps({"repository": REPO_URL}),
                headers={"Content-Type": "application/json"})
        finally:
            svc._fetcher = lambda *a: self.archive
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "no_documentation_files")

    def test_jsonl_logs_written(self):
        # trigger a scan (job log) and a request (access log), then inspect
        scan_status, _, _ = self.request(
            "POST", "/v1/scan",
            body=json.dumps({"repository": REPO_URL}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(scan_status, 200)
        status, _, _ = self.request("GET", "/health")
        access_lines = jsonl_lines(self.access_stream.getvalue())
        self.assertTrue(access_lines)
        # the server access-logs after sending each response, so entries from
        # the scan POST above may still be in flight; select the /health entry
        health_lines = [l for l in access_lines
                        if l["method"] == "GET" and l["path"] == "/health"]
        self.assertTrue(health_lines)
        last = health_lines[-1]
        self.assertEqual(last["event"], "access")
        self.assertEqual(last["status"], 200)
        self.assertIn("ts", last)
        self.assertIn("requestId", last)

        job_lines = jsonl_lines(self.job_stream.getvalue())
        events = {l["event"] for l in job_lines}
        self.assertIn("job_started", events)
        self.assertIn("job_completed", events)
        started = [l for l in job_lines if l["event"] == "job_started"][0]
        self.assertEqual(started["repository"], REPO_URL)


if __name__ == "__main__":
    unittest.main()
