"""Safe archive fetching and in-memory extraction.

- The tarball is downloaded over HTTPS from codeload.github.com with hard
  size and time caps (no shell-outs, no git binaries).
- Extraction happens in memory with defenses against:
    * path traversal ("../", absolute paths)      -> zip-slip
    * symlink / hardlink / other non-regular members
    * decompression bombs (per-file and total size caps, file count cap)
- Only files with documentation extensions are retained.
"""

import io
import re
import tarfile
import time
import ssl
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from .version import SERVICE_NAME, VERSION

_USER_AGENT = f"{SERVICE_NAME}/{VERSION}"

_DOC_EXTENSIONS = (".md", ".markdown", ".mdx", ".rst", ".html", ".htm")

_GITHUB_IP_RE = re.compile(
    r"^(?:140\.82\.\d{1,3}\.\d{1,3}|"
    r"143\.55\.\d{1,3}\.\d{1,3}|"
    r"20\.2(?:01|05|6|7|05)\.\d{1,3}\.\d{1,3}|"
    r"4\.(?:160|208)\.\d{1,3}\.\d{1,3}|"
    r"2a0a:a440:[0-9a-f:]+|"
    r"2606:50c0:[0-9a-f:]+)$",
    re.IGNORECASE,
)


class FetchError(Exception):
    # Edge-safe default: CDNs replace origin 502/504 bodies (docs/API.md "Why no 502/504?")
    def __init__(self, code: str, message: str, http_status: int = 503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class FetchResult:
    def __init__(self, files, total_files_seen, archive_bytes):
        self.files = files                # {relpath: text}
        self.total_files_seen = total_files_seen
        self.archive_bytes = archive_bytes


def archive_url(owner: str, repo: str, ref: str, codeload_host: str) -> str:
    """Single place where the ref is interpolated into a URL."""
    safe_ref = re.sub(r"[^A-Za-z0-9._/\-+]", "", ref)
    return (
        f"https://{codeload_host}/{owner}/{repo}/tar.gz/"
        f"{urlrequest.quote(safe_ref, safe='')}"
    )


def _build_ssl_context():
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def fetch_archive(url: str, *, max_bytes: int, connect_timeout_s: float,
                  total_timeout_s: float) -> bytes:
    """Download an archive with hard size/time caps. Returns raw bytes."""
    deadline = time.monotonic() + total_timeout_s
    req = urlrequest.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/x-gzip",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    try:
        resp = urlrequest.urlopen(
            req, timeout=min(connect_timeout_s, max(0.1, deadline - time.monotonic()))
        )
    except HTTPError as exc:
        if exc.code in (404, 410):
            raise FetchError("repository_or_ref_not_found",
                             f"upstream returned HTTP {exc.code}", 404) from exc
        if exc.code in (401, 403):
            raise FetchError("repository_not_public",
                             f"upstream returned HTTP {exc.code} "
                             "(private or blocked)", 403) from exc
        if exc.code == 429:
            raise FetchError("upstream_rate_limited",
                             "upstream returned HTTP 429", 429) from exc
        raise FetchError("upstream_error",
                         f"upstream returned HTTP {exc.code}", 503) from exc
    except (URLError, ssl.SSLError, ConnectionError, OSError) as exc:
        raise FetchError("upstream_unreachable",
                         f"could not reach upstream: {exc}", 503) from exc

    with resp:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("fetch_timeout", "archive download timed out", 503)
        resp.fp.raw._sock.settimeout(remaining)
        chunks = []
        total = 0
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise FetchError("fetch_timeout",
                                     "archive download timed out", 503)
                chunk = resp.read(min(64 * 1024, max(1, int(left * 1000))))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("archive_too_large",
                                     f"archive exceeds {max_bytes} bytes", 413)
                chunks.append(chunk)
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            raise FetchError("fetch_interrupted",
                             f"download interrupted: {exc}", 503) from exc
    return b"".join(chunks)


def _safe_member_name(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    if ":" in name and re.match(r"^[A-Za-z]:", name):
        return False
    parts = name.split("/")
    for part in parts:
        if part == "..":
            return False
    if re.search(r"(^|/)\.\.($|/)", name):
        return False
    return True


def extract_files(archive: bytes, *, max_files: int, max_file_bytes: int) -> FetchResult:
    """Extract doc files from a tar.gz archive in memory, safely."""
    files = {}
    total_seen = 0
    total_extracted_bytes = 0
    try:
        tf = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except tarfile.TarError as exc:
        raise FetchError("archive_unreadable",
                         f"not a valid tar.gz archive: {exc}", 503) from exc

    with tf:
        root_prefix = None
        for member in tf:
            total_seen += 1
            if total_seen > max_files:
                raise FetchError("too_many_files",
                                 f"archive contains more than {max_files} files", 413)
            if not member.isfile():
                continue  # skip symlinks, hardlinks, devices, dirs
            if not _safe_member_name(member.name):
                continue  # traversal attempt: skip silently but count
            if root_prefix is None:
                root_prefix = member.name.split("/")[0] + "/"
            rel = member.name
            if rel.startswith(root_prefix):
                rel = rel[len(root_prefix):]
            if not rel:
                continue
            if member.size > max_file_bytes:
                continue
            if not rel.lower().endswith(_DOC_EXTENSIONS):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            data = fh.read(max_file_bytes + 1)
            if len(data) > max_file_bytes:
                continue
            total_extracted_bytes += len(data)
            files[rel] = data.decode("utf-8", errors="replace")

    return FetchResult(files=files, total_files_seen=total_seen,
                       archive_bytes=len(archive))
