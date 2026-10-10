"""Subset scanner (pathPrefix) tests (1.12.0+).

The pathPrefix code path talks to api.github.com + raw.githubusercontent.com.
We mock both at the urllib boundary so the suite stays offline and fast.
"""

import json
import unittest
from unittest import mock

from docrot_scan_api import subset as subset_mod
from docrot_scan_api import service as service_mod
from docrot_scan_api.service import JobError, ScanService
from tests.helpers import make_config, simple_repo_tar, tar_bytes, FakeResponse


def _tree_json(entries, truncated=False):
    return json.dumps({"truncated": truncated, "tree": entries}).encode("utf-8")


class NormalizePrefixTests(unittest.TestCase):
    def test_default_is_docs(self):
        self.assertEqual(subset_mod.normalize_prefix(None), "docs/")

    def test_trims_and_adds_slash(self):
        self.assertEqual(subset_mod.normalize_prefix("/foo/"), "foo/")
        self.assertEqual(subset_mod.normalize_prefix("foo"), "foo/")
        self.assertEqual(subset_mod.normalize_prefix("foo/bar"), "foo/bar/")

    def test_rejects_dot_segments(self):
        with self.assertRaises(subset_mod.SubsetError) as cm:
            subset_mod.normalize_prefix("a/../b")
        self.assertEqual(cm.exception.code, "invalid_path_prefix")

    def test_rejects_query_chars(self):
        with self.assertRaises(subset_mod.SubsetError) as cm:
            subset_mod.normalize_prefix("foo?q=1")
        self.assertEqual(cm.exception.code, "invalid_path_prefix")

    def test_rejects_non_ascii(self):
        with self.assertRaises(subset_mod.SubsetError) as cm:
            subset_mod.normalize_prefix("f\u00f6o")
        self.assertEqual(cm.exception.code, "invalid_path_prefix")

    def test_empty_becomes_default(self):
        self.assertEqual(subset_mod.normalize_prefix(""), "docs/")
        self.assertEqual(subset_mod.normalize_prefix("   "), "docs/")


class FetchSubsetTests(unittest.TestCase):
    def test_walks_tree_and_fetches_raw(self):
        tree = _tree_json([
            {"type": "blob", "path": "docs/intro.md"},
            {"type": "blob", "path": "docs/setup.md"},
            {"type": "blob", "path": "src/ignored.py"},
            {"type": "tree", "path": "docs/sub"},
            {"type": "blob", "path": "docs/img.png"},
        ])
        raws = {
            "https://raw.githubusercontent.com/o/r/main/docs/intro.md":
                b"# Intro\n[ok](https://good.test/intro)\n",
            "https://raw.githubusercontent.com/o/r/main/docs/setup.md":
                b"# Setup\n[dead](https://bad.test/404)\n",
        }

        def fake_urlopen(req, timeout=10):
            url = req.full_url
            if "api.github.com" in url:
                return FakeResponse(status=200, body=tree,
                                    headers={"Content-Type": "application/json"})
            if url in raws:
                return FakeResponse(status=200, body=raws[url],
                                    headers={"Content-Type": "text/plain"})
            return FakeResponse(status=404, body=b"")

        import time as _t
        with mock.patch.object(subset_mod, "urlrequest") as ur:
            ur.Request.side_effect = lambda url, headers=None: mock.Mock(full_url=url)
            ur.urlopen.side_effect = fake_urlopen
            result = subset_mod.fetch_subset(
                "o", "r", "main", "docs",
                connect_timeout_s=5,
                total_deadline=_t.monotonic() + 30.0,
                max_file_bytes=64 * 1024,
                max_files=100,
                max_total_bytes=10 * 1024 * 1024,
            )
        self.assertEqual(set(result.files), {"docs/intro.md", "docs/setup.md"})
        self.assertEqual(result.raw_fetches, 2)
        self.assertFalse(result.truncated_tree)
        self.assertEqual(result.prefix, "docs/")

    def test_404_maps_to_repo_not_found(self):
        from urllib.error import HTTPError
        req_probe = mock.Mock(full_url="https://api.github.com/repos/o/r/git/trees/main?recursive=1")

        def fake_urlopen(req, timeout=10):
            raise HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

        import io
        with mock.patch.object(subset_mod, "urlrequest") as ur:
            ur.Request.side_effect = lambda url, headers=None: mock.Mock(full_url=url)
            ur.urlopen.side_effect = fake_urlopen
            with self.assertRaises(subset_mod.SubsetError) as cm:
                subset_mod.fetch_subset("o", "r", "main", "docs",
                                        connect_timeout_s=5,
                                        total_deadline=__import__("time").monotonic() + 30.0,
                                        max_file_bytes=64*1024,
                                        max_files=10,
                                        max_total_bytes=1024*1024)
        self.assertEqual(cm.exception.code, "repository_or_ref_not_found")

    def test_no_documentation_files(self):
        tree = _tree_json([
            {"type": "blob", "path": "src/code.py"},
            {"type": "blob", "path": "README.md"},
        ])

        def fake_urlopen(req, timeout=10):
            return FakeResponse(status=200, body=tree,
                                headers={"Content-Type": "application/json"})

        with mock.patch.object(subset_mod, "urlrequest") as ur:
            ur.Request.side_effect = lambda url, headers=None: mock.Mock(full_url=url)
            ur.urlopen.side_effect = fake_urlopen
            with self.assertRaises(subset_mod.SubsetError) as cm:
                subset_mod.fetch_subset("o", "r", "main", "docs",
                                        connect_timeout_s=5,
                                        total_deadline=__import__("time").monotonic() + 30.0,
                                        max_file_bytes=64*1024,
                                        max_files=10,
                                        max_total_bytes=1024*1024)
        self.assertEqual(cm.exception.code, "no_documentation_files")


    def test_large_tree_payload_not_truncated_by_read_cap(self):
        """Regression (1.12.1): recursive trees > 4 MB must parse fully.

        posthog/posthog hit this live: the capped read(4MB) produced a
        truncated JSON string and a 503 unparseable_upstream error instead
        of a scan. The body is served in small chunks to force the
        chunked-read path.
        """
        entries = [{"type": "blob", "path": f"docs/page_{i:05d}.md"}
                   for i in range(30)]
        tree = _tree_json(entries)
        # pad so the payload clearly exceeds the old 4 MB read cap
        tree = tree + b" " * (5 * 1024 * 1024 - len(tree) - 2) + b"\n"

        raws = {
            f"https://raw.githubusercontent.com/o/r/main/docs/page_{i:05d}.md":
                b"# p\n[ok](https://good.test/x)\n"
            for i in range(30)
        }

        def fake_urlopen(req, timeout=10):
            url = req.full_url
            if "api.github.com" in url:
                return FakeResponse(status=200, body=tree,
                                    headers={"Content-Type": "application/json"})
            if url in raws:
                return FakeResponse(status=200, body=raws[url],
                                    headers={"Content-Type": "text/plain"})
            return FakeResponse(status=404, body=b"")

        import time as _t
        with mock.patch.object(subset_mod, "urlrequest") as ur:
            ur.Request.side_effect = lambda url, headers=None: mock.Mock(full_url=url)
            ur.urlopen.side_effect = fake_urlopen
            result = subset_mod.fetch_subset(
                "o", "r", "main", "docs",
                connect_timeout_s=5,
                total_deadline=_t.monotonic() + 60.0,
                max_file_bytes=64 * 1024,
                max_files=100,
                max_total_bytes=64 * 1024 * 1024,
            )
        self.assertEqual(len(result.files), 30)


class ScanServiceSubsetTests(unittest.TestCase):
    def test_run_scan_subset_returns_doc_links(self):
        cfg = make_config()
        svc = ScanService(cfg, job_logger=None)

        subset_result = subset_mod.SubsetResult(
            files={
                "docs/intro.md": "# Intro\n[dead](https://bad.test/missing)\n",
                "docs/setup.md": "# Setup\n[ok](https://good.test/s)\n",
            },
            prefix="docs/", tree_api_hits=1, raw_fetches=2,
            truncated_tree=False,
        )
        svc._subsetter = mock.Mock(return_value=subset_result)
        outcomes = {
            "https://bad.test/missing": (404, "HTTP 404"),
            "https://good.test/s": (200, None),
        }
        svc._checker = mock.Mock(return_value={
            u: mock.Mock(ok=(c[0] == 200), status=c[0], error=c[1])
            for u, c in outcomes.items()
        })
        parsed = {"url": "https://github.com/o/r", "owner": "o", "repo": "r",
                  "ref": "main", "pathPrefix": "docs/"}
        resp = svc.run_scan(parsed, "req-sub-1")
        self.assertEqual(resp["mode"], "subset")
        self.assertEqual(resp["pathPrefix"], "docs/")
        self.assertEqual(resp["scannedFiles"], 2)
        self.assertEqual(resp["checkedUrls"], 2)
        self.assertEqual(len(resp["broken"]), 1)
        self.assertEqual(resp["broken"][0]["url"], "https://bad.test/missing")
        # receipt remains self-serve-ready
        self.assertIn("selfServe", resp["receipt"]["billing"])

    def test_subset_error_maps_to_job_error(self):
        cfg = make_config()
        svc = ScanService(cfg, job_logger=None)
        svc._subsetter = mock.Mock(side_effect=subset_mod.SubsetError(
            "no_documentation_files", "no docs", 422))
        parsed = {"url": "https://github.com/o/r", "owner": "o", "repo": "r",
                  "ref": "main", "pathPrefix": "README/"}
        with self.assertRaises(JobError) as cm:
            svc.run_scan(parsed, "req-sub-2")
        self.assertEqual(cm.exception.code, "no_documentation_files")
        self.assertEqual(cm.exception.http_status, 422)


if __name__ == "__main__":
    unittest.main()
