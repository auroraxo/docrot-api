"""Bounded-concurrency URL liveness checker.

For each URL:
  * vet scheme/host/port, resolve DNS via SSRF guard, pin the IP;
  * issue HEAD (falls back to GET on 405/501/403/429);
  * follow up to max_redirects manually, re-vetting every hop;
  * never read more than check_max_read_bytes of a body;
  * respect a global job deadline.

Results: {url: CheckOutcome}
"""

import concurrent.futures
import errno
import http.client
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urljoin

from .ssrf import vet_url, SSRFBlocked
from .version import SERVICE_NAME, VERSION

_FALLBACK_STATUSES = {405, 501, 403, 429}


class CheckOutcome:
    __slots__ = ("status", "error", "elapsed_ms")

    def __init__(self, status=None, error=None, elapsed_ms=0):
        self.status = status
        self.error = error
        self.elapsed_ms = elapsed_ms

    @property
    def ok(self):
        # any explicit error (timeout, ssrf_blocked, too_many_redirects,
        # non-http redirect scheme, HTTP >= 400) makes the check not-ok
        return self.error is None and self.status is not None \
            and 200 <= self.status < 400

    def as_dict(self):
        return {"status": self.status, "error": self.error,
                "elapsedMs": round(self.elapsed_ms)}


class Deadline:
    def __init__(self, seconds: float):
        self.expiry = time.monotonic() + seconds
        self._lock = threading.Lock()

    def remaining(self) -> float:
        with self._lock:
            return self.expiry - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0


def _connect_https_pinned(ip, hostname, port, timeout, ctx):
    """Open a TLS socket to `ip` while presenting SNI/hostname."""
    sock = socket.create_connection((ip, port), timeout=timeout)
    try:
        return ctx.wrap_socket(sock, server_hostname=hostname)
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def _new_connection(parts, ip: str, timeout: float) -> http.client.HTTPConnection:
    """Connection whose socket targets the vetted `ip`, with correct SNI/Host.

    https:  stdlib HTTPSConnection to the hostname (SNI/cert kept intact);
    the socket is later pinned to the vetted IP by _pin_connect_if_needed.
    http:   direct HTTPConnection to the vetted IP (Host header set
    explicitly in _check, so virtual-host routing is unaffected).
    """
    if parts.scheme == "https":
        return http.client.HTTPSConnection(parts.hostname, parts.port or 443,
                                           timeout=timeout)
    return http.client.HTTPConnection(ip, parts.port or 80, timeout=timeout)


def _pin_connect_if_needed(conn: http.client.HTTPConnection,
                           parts, ip: str, timeout: float) -> None:
    """Force the TLS socket to the vetted `ip` with correct SNI/hostname.

    Only applied when the connection will do its own DNS resolution through
    the stdlib class (detected via the attributes stdlib __init__ always
    sets); injected test doubles that skip stdlib __init__ are untouched.
    """
    if parts.scheme != "https":
        return
    if not getattr(conn, "host", None) or getattr(conn, "port", None) is None:
        return
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    def connect(c=conn, ip=ip, host=parts.hostname, port=parts.port or 443,
                t=timeout, ctx=ctx):
        c.sock = _connect_https_pinned(ip, host, port, t, ctx)

    conn.connect = connect


def check_url(url: str, *, deadline: Deadline, timeout: float,
              max_redirects: int, max_read_bytes: int,
              user_agent: str) -> CheckOutcome:
    start = time.monotonic()
    try:
        outcome = _check(url, deadline=deadline, timeout=timeout,
                         max_redirects=max_redirects,
                         max_read_bytes=max_read_bytes,
                         user_agent=user_agent)
    except SSRFBlocked as exc:
        return CheckOutcome(status=None, error=f"ssrf_blocked: {exc.message}",
                            elapsed_ms=(time.monotonic() - start) * 1000)
    except _JobTimeout:
        return CheckOutcome(status=None, error="timeout",
                            elapsed_ms=(time.monotonic() - start) * 1000)
    except (socket.timeout, TimeoutError) as exc:
        return CheckOutcome(status=None, error=f"timeout: {exc}",
                            elapsed_ms=(time.monotonic() - start) * 1000)
    except (http.client.HTTPException, ConnectionError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno in (errno.EHOSTUNREACH,
                                                      errno.ENETUNREACH):
            return CheckOutcome(status=None, error=f"unreachable: {exc}",
                                elapsed_ms=(time.monotonic() - start) * 1000)
        return CheckOutcome(status=None, error=f"network_error: {exc}",
                            elapsed_ms=(time.monotonic() - start) * 1000)
    return outcome


class _JobTimeout(Exception):
    pass


def _check(url: str, *, deadline: Deadline, timeout: float,
           max_redirects: int, max_read_bytes: int, user_agent: str):
    start = time.monotonic()
    current = url
    redirects = 0
    last_status = None
    while True:
        if deadline.expired():
            raise _JobTimeout()
        remaining = deadline.remaining()
        if remaining <= 0:
            raise _JobTimeout()
        parts = urlsplit(current)
        try:
            ip = vet_url(current, timeout=min(timeout, remaining))
        except SSRFBlocked:
            raise

        method = "HEAD"
        attempted_get = False
        while True:
            budget = min(timeout, deadline.remaining())
            if budget <= 0:
                raise _JobTimeout()
            conn = _new_connection(parts, ip, budget)
            try:
                _pin_connect_if_needed(conn, parts, ip, budget)
                conn.connect()
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                headers = {
                    "Host": parts.netloc,
                    "User-Agent": user_agent or f"{SERVICE_NAME}/{VERSION}",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                }
                conn.request(method, path, headers=headers)
                resp = conn.getresponse()
                # drain a bounded amount so the peer is not left hanging
                try:
                    resp.read(max_read_bytes)
                except Exception:
                    pass
                status = resp.status
                location = resp.getheader("Location")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            last_status = status
            if status in _FALLBACK_STATUSES and method == "HEAD" and not attempted_get:
                method = "GET"
                attempted_get = True
                continue
            break

        if 300 <= last_status < 400 and location:
            if redirects >= max_redirects:
                return CheckOutcome(status=last_status, error="too_many_redirects",
                                    elapsed_ms=(time.monotonic() - start) * 1000)
            nxt = urljoin(current, location)
            nparts = urlsplit(nxt)
            if nparts.scheme not in ("http", "https"):
                return CheckOutcome(status=last_status,
                                    error=f"redirect to non-http scheme: {nparts.scheme}",
                                    elapsed_ms=(time.monotonic() - start) * 1000)
            current = nxt
            redirects += 1
            continue

        ok = 200 <= last_status < 400
        return CheckOutcome(
            status=last_status,
            error=None if ok else f"HTTP {last_status}",
            elapsed_ms=(time.monotonic() - start) * 1000,
        )


def check_urls(urls, *, deadline: Deadline, concurrency: int, timeout: float,
               max_redirects: int, max_read_bytes: int,
               user_agent: str, on_result=None):
    """Check many URLs with bounded concurrency. Returns {url: CheckOutcome}."""
    results = {}
    if not urls:
        return results
    concurrency = max(1, min(concurrency, len(urls)))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="docrot-check"
    ) as pool:
        futures = {
            pool.submit(
                check_url, url,
                deadline=deadline, timeout=timeout,
                max_redirects=max_redirects,
                max_read_bytes=max_read_bytes,
                user_agent=user_agent,
            ): url
            for url in urls
        }
        for fut in concurrent.futures.as_completed(futures):
            url = futures[fut]
            try:
                outcome = fut.result()
            except Exception as exc:  # never let one URL kill the job
                outcome = CheckOutcome(status=None, error=f"internal_error: {exc}")
            results[url] = outcome
            if on_result:
                on_result(url, outcome)
    return results
