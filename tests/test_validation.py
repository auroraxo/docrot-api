"""T1-T6, T6b: repository and ref validation."""

import unittest

from docrot_scan_api import github


class RepositoryValidationTests(unittest.TestCase):
    def test_reject_http_scheme(self):
        with self.assertRaises(github.RepositoryValidationError) as cm:
            github.normalize_repository_url("http://github.com/o/r")
        self.assertEqual(cm.exception.code, "unsupported_repository_scheme")

    def test_reject_other_host(self):
        with self.assertRaises(github.RepositoryValidationError) as cm:
            github.normalize_repository_url("https://gitlab.com/o/r")
        self.assertEqual(cm.exception.code, "unsupported_repository_host")

    def test_reject_missing_repo(self):
        with self.assertRaises(github.RepositoryValidationError) as cm:
            github.normalize_repository_url("https://github.com/norepo")
        self.assertEqual(cm.exception.code, "invalid_repository")

    def test_reject_credentials(self):
        with self.assertRaises(github.RepositoryValidationError) as cm:
            github.normalize_repository_url("https://user:pw@github.com/o/r")
        self.assertEqual(cm.exception.code, "invalid_repository")

    def test_reject_deep_tree_path(self):
        with self.assertRaises(github.RepositoryValidationError) as cm:
            github.normalize_repository_url("https://github.com/o/r/tree/dev/docs")
        self.assertEqual(cm.exception.code, "invalid_repository")

    def test_accept_plain(self):
        r = github.normalize_repository_url("https://github.com/owner/repo")
        self.assertEqual(r["url"], "https://github.com/owner/repo")
        self.assertEqual((r["owner"], r["repo"], r["ref"]), ("owner", "repo", None))

    def test_accept_tree_ref(self):
        r = github.normalize_repository_url("https://github.com/o/r/tree/v1.2.3")
        self.assertEqual(r["ref"], "v1.2.3")
        self.assertEqual(r["url"], "https://github.com/o/r")

    def test_accept_blob_ref(self):
        r = github.normalize_repository_url("https://github.com/o/r/blob/fix-1")
        self.assertEqual(r["ref"], "fix-1")

    def test_git_suffix_stripped(self):
        r = github.normalize_repository_url("https://github.com/o/r.git")
        self.assertEqual(r["repo"], "r")

    def test_canonical_url_uses_default_host(self):
        r = github.normalize_repository_url("https://www.github.com/o/r")
        self.assertEqual(r["url"], "https://github.com/o/r")

    def test_reject_query_and_fragment(self):
        for bad in ("https://github.com/o/r?x=1", "https://github.com/o/r#frag"):
            with self.assertRaises(github.RepositoryValidationError):
                github.normalize_repository_url(bad)

    def test_reject_reserved_names(self):
        with self.assertRaises(github.RepositoryValidationError):
            github.normalize_repository_url("https://github.com/o/settings")

    def test_reject_bad_owner_shape(self):
        for bad in ("https://github.com/-bad/r", "https://github.com/o-/r",
                    "https://github.com/.hidden/r"):
            with self.assertRaises(github.RepositoryValidationError) as cm:
                github.normalize_repository_url(bad)
            self.assertEqual(cm.exception.code, "invalid_repository")

    def test_reject_non_string_and_empty(self):
        for bad in (None, "", "   ", 123):
            with self.assertRaises(github.RepositoryValidationError):
                github.normalize_repository_url(bad)


class RefValidationTests(unittest.TestCase):
    def test_reject_git_option_injection(self):
        with self.assertRaises(github.RepositoryValidationError) as cm:
            github.validate_ref("--upload-pack=evil")
        self.assertEqual(cm.exception.code, "invalid_ref")

    def test_reject_traversal(self):
        for bad in ("../../etc", "main/../x", "a//b", "%2e%2e", "a\\b"):
            with self.assertRaises(github.RepositoryValidationError) as cm:
                github.validate_ref(bad)
            self.assertEqual(cm.exception.code, "invalid_ref")

    def test_reject_reserved_forms(self):
        for bad in ("refs/heads/main", "HEAD", "heads", "tags"):
            with self.assertRaises(github.RepositoryValidationError):
                github.validate_ref(bad)

    def test_reject_too_long(self):
        with self.assertRaises(github.RepositoryValidationError):
            github.validate_ref("a" * 201)

    def test_accept_normal_refs(self):
        for good in ("main", "v1.0.0", "feature/x-1", "release_2", "abc+def"):
            github.validate_ref(good)


if __name__ == "__main__":
    unittest.main()
