"""T18-T19, T22b: ScanService orchestration with mocked fetch+network,
receipt shape, job errors, and JSONL job logging."""

import json
import tempfile
import unittest
from unittest import mock

from docrot_scan_api import service as service_mod
from docrot_scan_api.jsonl import JsonlLogger
from docrot_scan_api.service import JobError, ScanService
from tests.helpers import FakeResponse, make_config, simple_repo_tar


class _CheckerStub:
    """Replaces checker.check_urls to avoid any real sockets."""

    def __init__(self, outcomes):
        self.outcomes = outcomes  # {url: (status, error)}
        self.calls = []

    def __call__(self, urls, **kwargs):
        self.calls.append({"urls": urls, "kwargs": kwargs})
        from docrot_scan_api.checker import CheckOutcome
        return {u: CheckOutcome(status=s, error=e)
                for u, (s, e) in self.outcomes.items()}


def make_service(tar=simple_repo_tar(), outcomes=None, job_log=None):
    cfg = make_config()
    log = JsonlLogger("job-test", stream=open_temp(job_log)) if job_log else None
    stub = _CheckerStub(outcomes or {})
    svc = ScanService(cfg, job_logger=log, checker=stub)
    svc._fetcher = lambda owner, repo, ref: tar
    return svc, stub, cfg


def open_temp(path):
    return open(path, "a", encoding="utf-8")


REPO = {"url": "https://github.com/o/r", "owner": "o", "repo": "r", "ref": None}


class ScanServiceTests(unittest.TestCase):
    def test_success_response_and_receipt(self):
        outcomes = {
            "https://bad.test/gone": (404, "HTTP 404"),
            "https://good.test/page": (200, None),
            "https://good.test/rst": (200, None),
            "https://bad.test/dead-page": (None, "timeout"),
        }
        svc, stub, cfg = make_service(outcomes=outcomes)
        resp = svc.run_scan(REPO, "req-1")

        self.assertEqual(resp["repository"], "https://github.com/o/r")
        self.assertIsNone(resp["ref"])
        self.assertEqual(resp["scannedFiles"], 2)
        self.assertEqual(resp["checkedUrls"], 4)
        self.assertEqual(resp["requestId"], "req-1")
        self.assertIsInstance(resp["durationMs"], int)

        broken = {(b["url"], b["source"], b["line"]): b for b in resp["broken"]}
        self.assertEqual(len(resp["broken"]), 2)
        b1 = broken[("https://bad.test/gone", "README.md", 2)]
        self.assertEqual(b1["status"], 404)
        self.assertEqual(b1["error"], "HTTP 404")
        b2 = broken[("https://bad.test/dead-page", "docs/guide.rst", 2)]
        self.assertIsNone(b2["status"])
        self.assertEqual(b2["error"], "timeout")

        receipt = resp["receipt"]
        self.assertEqual(receipt["kind"], "scan-completed")
        self.assertEqual(receipt["requestId"], "req-1")
        self.assertEqual(receipt["brokenCount"], 2)
        self.assertEqual(receipt["billing"]["model"], "manual-invoicing-pilot")
        self.assertEqual(receipt["billing"]["amountDueUsd"], 1.0)
        self.assertEqual(receipt["billing"]["payTo"],
                         "CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn")
        self.assertEqual(receipt["billing"]["currency"], "SOL")
        self.assertIn("manual", receipt["billing"]["terms"])
        self.assertIn("completedAt", receipt)

    def test_same_url_two_sources_both_listed(self):
        tar = simple_repo_tar() + b""  # reuse
        archive = tar
        from tests.helpers import tar_bytes
        tar2 = tar_bytes([
            ("repo/a.md", "f", "[x](https://dup.test/1)\n"),
            ("repo/b.md", "f", "see https://dup.test/1 ok\n"),
        ])
        outcomes = {"https://dup.test/1": (500, "HTTP 500")}
        svc, stub, cfg = make_service(tar=tar2, outcomes=outcomes)
        resp = svc.run_scan(REPO, "req-2")
        self.assertEqual(len(resp["broken"]), 2)
        self.assertEqual({b["source"] for b in resp["broken"]},
                         {"a.md", "b.md"})
        self.assertEqual(resp["checkedUrls"], 1)

    def test_too_many_urls_job_error(self):
        from tests.helpers import tar_bytes
        members = [(f"repo/f{i}.md", "f",
                    f"[a](https://u{i}.test/x)\n") for i in range(10)]
        svc, stub, cfg = make_service(tar=tar_bytes(members))
        cfg.max_urls = 5
        with self.assertRaises(JobError) as cm:
            svc.run_scan(REPO, "req-3")
        self.assertEqual(cm.exception.code, "too_many_urls")
        self.assertEqual(cm.exception.http_status, 413)

    def test_no_documentation_files(self):
        from tests.helpers import tar_bytes
        tar = tar_bytes([("repo/code.py", "f", "x = 1\n")])
        svc, stub, cfg = make_service(tar=tar)
        with self.assertRaises(JobError) as cm:
            svc.run_scan(REPO, "req-4")
        self.assertEqual(cm.exception.code, "no_documentation_files")
        self.assertEqual(cm.exception.http_status, 422)

    def test_fetch_error_propagates_as_job_error(self):
        cfg = make_config()
        svc = ScanService(cfg)
        from docrot_scan_api.extract import FetchError
        svc._fetcher = lambda *a: (_ for _ in ()).throw(
            FetchError("repository_or_ref_not_found", "upstream 404", 404))
        with self.assertRaises(JobError) as cm:
            svc.run_scan(REPO, "req-5")
        self.assertEqual(cm.exception.code, "repository_or_ref_not_found")
        self.assertEqual(cm.exception.http_status, 404)

    def test_fetcher_receives_codeload_ref(self):
        cfg = make_config()
        svc = ScanService(cfg)
        seen = {}

        def fake_fetch(owner, repo, ref):
            seen["args"] = (owner, repo, ref)
            from tests.helpers import simple_repo_tar
            return simple_repo_tar()

        stub = _CheckerStub({})
        svc._fetcher = fake_fetch
        svc._checker = stub
        repo = {"url": "https://github.com/o/r", "owner": "o",
                "repo": "r", "ref": "v2"}
        svc.run_scan(repo, "req-6")
        self.assertEqual(seen["args"], ("o", "r", "v2"))

    def test_deadline_during_extraction(self):
        cfg = make_config()
        cfg.max_job_seconds = 0  # immediately expired
        svc = ScanService(cfg)
        svc._fetcher = lambda *a: simple_repo_tar()
        with self.assertRaises(JobError) as cm:
            svc.run_scan(REPO, "req-7")
        self.assertEqual(cm.exception.code, "duration_exceeded")
        self.assertEqual(cm.exception.http_status, 503)

    def test_job_logger_jsonl(self):
        import io
        stream = io.StringIO()
        cfg = make_config()
        log = JsonlLogger("job", stream=stream)
        all_ok = {u: (200, None) for u in (
            "https://bad.test/gone", "https://good.test/page",
            "https://good.test/rst", "https://bad.test/dead-page")}
        stub = _CheckerStub(all_ok)
        svc = ScanService(cfg, job_logger=log, checker=stub)
        svc._fetcher = lambda *a: simple_repo_tar()
        resp = svc.run_scan(REPO, "req-8")
        lines = [json.loads(l) for l in stream.getvalue().strip().splitlines()]
        events = [l["event"] for l in lines]
        self.assertIn("job_started", events)
        self.assertIn("links_extracted", events)
        self.assertIn("job_completed", events)
        completed = [l for l in lines if l["event"] == "job_completed"][0]
        self.assertEqual(completed["requestId"], "req-8")
        self.assertEqual(completed["brokenCount"], 0)
        self.assertEqual(completed["brokenCount"], len(resp["broken"]))


if __name__ == "__main__":
    unittest.main()
