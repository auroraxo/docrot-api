"""T9-T13: archive fetching (mocked) and safe extraction."""

import io
import unittest
from unittest import mock

from docrot_scan_api import extract as extract_mod
from tests.helpers import make_config, simple_repo_tar, tar_bytes


class FetchArchiveTests(unittest.TestCase):
    def _patch_urlopen(self, fake):
        return mock.patch("docrot_scan_api.extract.urlrequest.urlopen", fake)

    def test_success_returns_bytes(self):
        archive = simple_repo_tar()
        fake = mock.MagicMock()
        fake.status = 200
        fake.__enter__ = lambda s: fake
        fake.__exit__ = lambda s, *a: False
        fake.read.side_effect = [archive, b""]
        fake.fp.raw._sock = mock.MagicMock()
        with self._patch_urlopen(mock.MagicMock(return_value=fake)):
            data = extract_mod.fetch_archive(
                "https://codeload.github.com/o/r/tar.gz/HEAD",
                max_bytes=10 * 1024 * 1024, connect_timeout_s=5,
                total_timeout_s=10)
        self.assertEqual(data, archive)

    def test_404_maps_to_repository_or_ref_not_found(self):
        import urllib.error

        def fake(url, timeout):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        with self._patch_urlopen(fake):
            with self.assertRaises(extract_mod.FetchError) as cm:
                extract_mod.fetch_archive(
                    "https://codeload.github.com/o/r/tar.gz/HEAD",
                    max_bytes=1000, connect_timeout_s=1, total_timeout_s=5)
        self.assertEqual(cm.exception.code, "repository_or_ref_not_found")

    def test_403_maps_to_repository_not_public(self):
        import urllib.error

        def fake(url, timeout):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {},
                                         io.BytesIO())

        with self._patch_urlopen(fake):
            with self.assertRaises(extract_mod.FetchError) as cm:
                extract_mod.fetch_archive(
                    "https://codeload.github.com/o/r/tar.gz/HEAD",
                    max_bytes=1000, connect_timeout_s=1, total_timeout_s=5)
        self.assertEqual(cm.exception.code, "repository_not_public")

    def test_oversize_archive_rejected(self):
        archive = simple_repo_tar()
        fake = mock.MagicMock()
        fake.status = 200
        fake.__enter__ = lambda s: fake
        fake.__exit__ = lambda s, *a: False
        fake.read.side_effect = [archive, b""]
        fake.fp.raw._sock = mock.MagicMock()
        with self._patch_urlopen(mock.MagicMock(return_value=fake)):
            with self.assertRaises(extract_mod.FetchError) as cm:
                extract_mod.fetch_archive(
                    "https://codeload.github.com/o/r/tar.gz/HEAD",
                    max_bytes=10, connect_timeout_s=1, total_timeout_s=5)
        self.assertEqual(cm.exception.code, "archive_too_large")

    def test_timeout(self):
        def fake(url, timeout):
            raise TimeoutError("too slow")

        with self._patch_urlopen(fake):
            with self.assertRaises(extract_mod.FetchError) as cm:
                extract_mod.fetch_archive(
                    "https://codeload.github.com/o/r/tar.gz/HEAD",
                    max_bytes=1000, connect_timeout_s=1, total_timeout_s=5)
        self.assertEqual(cm.exception.code, "upstream_unreachable")


class ExtractionSafetyTests(unittest.TestCase):
    def test_extracts_doc_files_and_skips_others(self):
        archive = simple_repo_tar()
        result = extract_mod.extract_files(archive, max_files=100,
                                           max_file_bytes=1000)
        self.assertEqual(sorted(result.files),
                         ["README.md", "docs/guide.rst"])
        self.assertNotIn("src/code.py", result.files)

    def test_zip_slip_member_skipped(self):
        archive = tar_bytes([
            ("repo/ok.md", "f", "[a](https://good.test/1)\n"),
            ("repo/../escape.md", "f", "should not appear"),
            ("/abs.md", "f", "should not appear"),
            ("repo/sub/../../up.md", "f", "should not appear"),
        ])
        result = extract_mod.extract_files(archive, max_files=100,
                                           max_file_bytes=1000)
        self.assertEqual(list(result.files), ["ok.md"])

    def test_symlink_and_hardlink_skipped(self):
        archive = tar_bytes([
            ("repo/ok.md", "f", "[a](https://good.test/1)\n"),
            ("repo/evil.md", "l", "/etc/passwd"),
            ("repo/hard.md", "h", "/etc/shadow"),
        ])
        result = extract_mod.extract_files(archive, max_files=100,
                                           max_file_bytes=1000)
        self.assertEqual(list(result.files), ["ok.md"])

    def test_file_count_cap(self):
        members = [(f"repo/f{i}.md", "f", "x") for i in range(20)]
        archive = tar_bytes(members)
        with self.assertRaises(extract_mod.FetchError) as cm:
            extract_mod.extract_files(archive, max_files=10,
                                      max_file_bytes=1000)
        self.assertEqual(cm.exception.code, "too_many_files")

    def test_oversized_file_skipped(self):
        archive = tar_bytes([
            ("repo/big.md", "f", "x" * 5000),
            ("repo/ok.md", "f", "y"),
        ])
        result = extract_mod.extract_files(archive, max_files=100,
                                           max_file_bytes=1000)
        self.assertEqual(list(result.files), ["ok.md"])

    def test_invalid_archive_raises_archive_unreadable(self):
        with self.assertRaises(extract_mod.FetchError) as cm:
            extract_mod.extract_files(b"this is not a tarball",
                                      max_files=10, max_file_bytes=1000)
        self.assertEqual(cm.exception.code, "archive_unreadable")

    def test_archive_url_quotes_ref(self):
        url = extract_mod.archive_url("o", "r", "feat/x-1", "codeload.github.com")
        self.assertEqual(url,
                         "https://codeload.github.com/o/r/tar.gz/feat%2Fx-1")
        url2 = extract_mod.archive_url("o", "r", "HEAD", "codeload.github.com")
        self.assertEqual(url2, "https://codeload.github.com/o/r/tar.gz/HEAD")


if __name__ == "__main__":
    unittest.main()
