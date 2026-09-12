"""T15-T19: URL checking with mocked sockets/SSL and SSRF guard tests."""

import socket
import threading
import unittest
from unittest import mock

from docrot_scan_api import checker, ssrf
from docrot_scan_api.checker import CheckOutcome, Deadline, check_url


def make_deadline(seconds=10.0):
    return Deadline(seconds)


def patch_dns(addr="93.184.216.34"):
    """Patch getaddrinfo so hosts resolve to a public IP without network."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 80))]
    return mock.patch("docrot_scan_api.ssrf.socket.getaddrinfo",
                      side_effect=fake_getaddrinfo)


class FakeHTTPConn:
    """Stands in for http.client connections opened by _open_connection."""

    responses = []  # list of (status, headers, body) per request
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        pass

    def close(self):
        pass

    def request(self, method, path, headers=None):
        FakeHTTPConn.requests.append((method, path, dict(headers or {})))

    def getresponse(self):
        status, headers, body = FakeHTTPConn.responses.pop(0)
        r = mock.MagicMock()
        r.status = status
        r.getheader = lambda name, default=None: headers.get(name, default)
        r.read = lambda amt=-1: body
        return r


class CheckerTests(unittest.TestCase):
    def setUp(self):
        FakeHTTPConn.requests = []
        FakeHTTPConn.responses = []

    def _run(self, url="https://target.test/page", responses=None):
        FakeHTTPConn.responses = responses or [(200, {}, b"")]
        with patch_dns(), \
             mock.patch("docrot_scan_api.checker.http.client.HTTPConnection",
                        FakeHTTPConn), \
             mock.patch("docrot_scan_api.checker.http.client.HTTPSConnection",
                        FakeHTTPConn):
            return check_url(url, deadline=make_deadline(), timeout=2.0,
                             max_redirects=3, max_read_bytes=1024,
                             user_agent="test-agent")

    def test_200_ok(self):
        outcome = self._run()
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.status, 200)
        self.assertIsNone(outcome.error)

    def test_404_broken(self):
        outcome = self._run(responses=[(404, {}, b"")])
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, 404)
        self.assertEqual(outcome.error, "HTTP 404")

    def test_head_405_falls_back_to_get(self):
        outcome = self._run(responses=[(405, {}, b""), (200, {}, b"")])
        self.assertTrue(outcome.ok)
        methods = [m for m, _, _ in FakeHTTPConn.requests]
        self.assertEqual(methods, ["HEAD", "GET"])

    def test_head_501_falls_back_to_get_404(self):
        outcome = self._run(responses=[(501, {}, b""), (404, {}, b"")])
        self.assertEqual(outcome.status, 404)
        self.assertFalse(outcome.ok)

    def test_redirect_followed_and_revetted(self):
        outcome = self._run(responses=[
            (301, {"Location": "https://target.test/moved"}, b""),
            (200, {}, b""),
        ])
        self.assertTrue(outcome.ok)

    def test_redirect_loop_reported(self):
        outcome = self._run(responses=[
            (301, {"Location": "https://target.test/b"}, b"")
            for _ in range(6)
        ])
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "too_many_redirects")

    def test_redirect_to_non_http_scheme(self):
        outcome = self._run(responses=[
            (302, {"Location": "ftp://target.test/x"}, b"")
        ])
        self.assertIn("non-http", outcome.error)

    def test_concurrency_bound(self):
        urls = [f"https://u{i}.test/x" for i in range(30)]
        active = {"n": 0, "max": 0}
        lock = threading.Lock()

        class SlowConn(FakeHTTPConn):
            def request(self, *a, **k):
                with lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                import time
                time.sleep(0.02)
                super().request(*a, **k)

            def getresponse(self):
                with lock:
                    active["n"] -= 1
                return super().getresponse()

        FakeHTTPConn.responses = [(200, {}, b"")] * 30
        with patch_dns(), \
             mock.patch("docrot_scan_api.checker.http.client.HTTPConnection", SlowConn), \
             mock.patch("docrot_scan_api.checker.http.client.HTTPSConnection", SlowConn):
            results = checker.check_urls(urls, deadline=make_deadline(30),
                                         concurrency=4, timeout=2.0,
                                         max_redirects=3,
                                         max_read_bytes=1024,
                                         user_agent="t")
        self.assertEqual(len(results), 30)
        self.assertLessEqual(active["max"], 4)
        self.assertTrue(all(o.ok for o in results.values()))


class SSRFTests(unittest.TestCase):
    def _blocked(self, addr):
        def fake_getaddrinfo(host, port, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 80))]
        with mock.patch("docrot_scan_api.ssrf.socket.getaddrinfo",
                        side_effect=fake_getaddrinfo):
            with self.assertRaises(ssrf.SSRFBlocked):
                ssrf.vet_url("https://innocent.test/x")

    def test_loopback_blocked(self):
        self._blocked("127.0.0.1")

    def test_private_blocked(self):
        self._blocked("10.1.2.3")
        self._blocked("192.168.1.10")
        self._blocked("172.16.0.5")

    def test_link_local_blocked(self):
        self._blocked("169.254.169.254")

    def test_reserved_blocked(self):
        self._blocked("240.0.0.1")

    def test_mapped_ipv6_blocked(self):
        with mock.patch("docrot_scan_api.ssrf.socket.getaddrinfo",
                        side_effect=lambda *a, **k: [
                            (socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                             ("::ffff:10.0.0.1", 80, 0, 0))]):
            with self.assertRaises(ssrf.SSRFBlocked):
                ssrf.vet_host("mapped.test")

    def test_dns_failure_blocked(self):
        def fail(*a, **k):
            raise socket.gaierror("no dns")
        with mock.patch("docrot_scan_api.ssrf.socket.getaddrinfo",
                        side_effect=fail):
            with self.assertRaises(ssrf.SSRFBlocked):
                ssrf.vet_host("nope.test")

    def test_literal_private_ip_blocked(self):
        with self.assertRaises(ssrf.SSRFBlocked):
            ssrf.vet_host("127.0.0.1")
        with self.assertRaises(ssrf.SSRFBlocked):
            ssrf.vet_host("::1")
        with self.assertRaises(ssrf.SSRFBlocked):
            ssrf.vet_host("169.254.169.254")

    def test_nonstandard_port_blocked(self):
        with self.assertRaises(ssrf.SSRFBlocked):
            ssrf.vet_url("https://host.test:8443/x")

    def test_public_ip_allowed(self):
        def fake_getaddrinfo(host, port, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34", 80))]
        with mock.patch("docrot_scan_api.ssrf.socket.getaddrinfo",
                        side_effect=fake_getaddrinfo):
            ip = ssrf.vet_host("example.test")
        self.assertEqual(ip, "93.184.216.34")

    def test_ssrf_block_surfaces_in_check_result(self):
        def fake_getaddrinfo(host, port, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", 80))]
        with mock.patch("docrot_scan_api.ssrf.socket.getaddrinfo",
                        side_effect=fake_getaddrinfo):
            outcome = check_url("https://innocent.test/x",
                                deadline=make_deadline(), timeout=1.0,
                                max_redirects=3, max_read_bytes=100,
                                user_agent="t")
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.error.startswith("ssrf_blocked"))

    def test_ssrf_block_via_redirect_is_caught(self):
        """Redirect to an internal address must be blocked too."""
        FakeHTTPConn.responses = [(302, {"Location": "http://127.0.0.1:9/x"}, b"")]

        class DNSPlus:
            """Resolve first host public, second (loopback literal) vetted."""

            def __init__(self):
                self.calls = 0

            def __call__(self, host, port, *a, **k):
                self.calls += 1
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                         ("93.184.216.34", 80))]

        with patch_dns(), \
             mock.patch("docrot_scan_api.checker.http.client.HTTPConnection", FakeHTTPConn), \
             mock.patch("docrot_scan_api.checker.http.client.HTTPSConnection", FakeHTTPConn):
            outcome = check_url("https://redirector.test/x",
                                deadline=make_deadline(), timeout=2.0,
                                max_redirects=3, max_read_bytes=100,
                                user_agent="t")
        # literal 127.0.0.1 target is blocked by the literal-IP guard
        self.assertTrue(outcome.error.startswith("ssrf_blocked"))


class DeadlineTests(unittest.TestCase):
    def test_expired_deadline_yields_timeout(self):
        outcome = check_url("https://slow.test/x", deadline=Deadline(-1),
                            timeout=1.0, max_redirects=3,
                            max_read_bytes=100, user_agent="t")
        self.assertEqual(outcome.error, "timeout")

    def test_check_outcome_shape(self):
        o = CheckOutcome(status=404, error="HTTP 404", elapsed_ms=12.4)
        self.assertEqual(o.as_dict(),
                         {"status": 404, "error": "HTTP 404", "elapsedMs": 12})


if __name__ == "__main__":
    unittest.main()
