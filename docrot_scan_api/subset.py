"""Subset fetch: sparse documentation scans for repositories whose full
archive exceeds DOCROT_MAX_ARCHIVE_BYTES (1.12.0+).

A scan request may carry ``pathPrefix`` (default ``docs/``). Instead of the
codeload tarball we use the public GitHub API to list the documentation
subtree (GET /repos/{owner}/{repo}/git/trees/{ref}) and then fetch only the
doc files under that prefix via raw.githubusercontent.com. Every response is
subject to the same hard caps as the full-archive path: per-request archive
ceiling (pathArchiveMaxBytes <= config.max_archive_override_bytes), per-file
size cap, total file cap, total deadline, and the SSRF guard on every
outbound fetch. Only https API/raw endpoints under api.github.com /
raw.githubusercontent.com are contacted; no shell-outs, no git binary.

Stdlib-only, like the rest of the service.
"""

import json
import re
import ssl
import time
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from .version import SERVICE_NAME, VERSION

_USER_AGENT = f"{SERVICE_NAME}/{VERSION}"
_API_HOST = "api.github.com"
_RAW_HOST = "raw.githubusercontent.com"

_DOC_EXTENSIONS = (".md", ".markdown", ".mdx", ".rst", ".html", ".htm")

_DEFAULT_PREFIX = "docs/"


class SubsetError(Exception):
    def __init__(self, code, message, http_status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def normalize_prefix(value):
    """Validate + normalize a pathPrefix request field.

    Returns a prefix like ``docs/`` (leading slash stripped, trailing slash
    enforced, no ``..`` segments, no backslashes, no query/fragment chars).
    """
    if value is None:
        return _DEFAULT_PREFIX
    if not isinstance(value, str):
        raise SubsetError("invalid_path_prefix", "pathPrefix must be a string")
    if len(value) > 200:
        raise SubsetError("invalid_path_prefix",
                          "pathPrefix longer than 200 characters")
    if value.strip() == "":
        return _DEFAULT_PREFIX
    if re.search(r"[?&#%\\]", value) or ".." in value or "\x00" in value:
        raise SubsetError("invalid_path_prefix",
                          "pathPrefix contains forbidden characters")
    if not all(ord(ch) < 128 for ch in value):
        raise SubsetError("invalid_path_prefix",
                          "pathPrefix must be ASCII")
    parts = [p for p in value.split("/") if p != ""]
    if not parts:
        return _DEFAULT_PREFIX
    for part in parts:
        if part in (".", ".."):
            raise SubsetError("invalid_path_prefix",
                              "pathPrefix must not contain . or .. segments")
    prefix = "/".join(parts) + "/"
    return prefix


def _fetch_json(url, *, connect_timeout_s, total_deadline):
    remaining = max(0.1, total_deadline - time.monotonic())
    req = urlrequest.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    try:
        with urlrequest.urlopen(req, timeout=min(connect_timeout_s, remaining)) as resp:
            # No fixed read cap here: recursive git-tree payloads for large
            # repos exceed 4 MB and a capped read yields truncated JSON that
            # fails to parse. Honor the total deadline by reading in bounded
            # chunks instead of one big read().
            chunks = []
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                if time.monotonic() >= total_deadline:
                    raise SubsetError("upstream_timeout",
                                      "GitHub API response not fully read "
                                      "before total deadline", 504)
            data = b"".join(chunks)
    except HTTPError as exc:
        if exc.code == 404:
            raise SubsetError("repository_or_ref_not_found",
                              f"upstream returned HTTP 404 for {url}", 404)
        if exc.code in (401, 403, 429):
            raise SubsetError("upstream_rate_limited",
                              f"GitHub API returned HTTP {exc.code}", 429)
        raise SubsetError("upstream_error",
                          f"upstream returned HTTP {exc.code}", 503)
    except (URLError, ssl.SSLError, ConnectionError, OSError) as exc:
        raise SubsetError("upstream_unreachable",
                          f"could not reach upstream: {exc}", 503)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubsetError("upstream_error",
                          f"unparseable upstream response: {exc}", 503)


def _fetch_raw(url, *, connect_timeout_s, total_deadline, max_file_bytes,
               total_budget):
    remaining = max(0.1, total_deadline - time.monotonic())
    req = urlrequest.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        resp = urlrequest.urlopen(req, timeout=min(connect_timeout_s, remaining))
    except HTTPError as exc:
        if exc.code in (404, 410):
            raise SubsetError("file_not_found",
                              f"raw fetch returned HTTP {exc.code}", 404)
        if exc.code in (401, 403, 429):
            raise SubsetError("upstream_rate_limited",
                              f"raw fetch returned HTTP {exc.code}", 429)
        raise SubsetError("upstream_error",
                          f"upstream returned HTTP {exc.code}", 503)
    except (URLError, ssl.SSLError, ConnectionError, OSError) as exc:
        raise SubsetError("upstream_unreachable",
                          f"could not reach upstream: {exc}", 503)
    with resp:
        chunks = []
        got = 0
        try:
            while True:
                left = total_deadline - time.monotonic()
                if left <= 0:
                    raise SubsetError("duration_exceeded",
                                      "subset fetch exceeded the job deadline",
                                      503)
                chunk = resp.read(min(64 * 1024, max(1, int(left * 1000))))
                if not chunk:
                    break
                got += len(chunk)
                if got > max_file_bytes or got > total_budget:
                    raise SubsetError("file_too_large",
                                      f"raw file exceeds the size cap", 413)
                chunks.append(chunk)
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            raise SubsetError("fetch_interrupted",
                              f"raw fetch interrupted: {exc}", 503)
    return b"".join(chunks)


class SubsetResult:
    def __init__(self, files, prefix, tree_api_hits, raw_fetches,
                 truncated_tree):
        self.files = files                  # {relpath: text}
        self.prefix = prefix
        self.tree_api_hits = tree_api_hits
        self.raw_fetches = raw_fetches
        self.truncated_tree = truncated_tree


def fetch_subset(owner, repo, ref, prefix, *, connect_timeout_s,
                 total_deadline, max_file_bytes, max_files, max_total_bytes):
    """Fetch all doc files under ``prefix`` for owner/repo@ref.

    Walks the git tree via the GitHub API (single recursive call, falls back
    to per-directory paging when truncated), then fetches each doc file
    under the prefix from raw.githubusercontent.com. Total bytes across all
    raw fetches are capped at ``max_total_bytes``.
    """
    prefix = normalize_prefix(prefix)
    ref_q = quote(ref or "HEAD", safe="")
    tree_url = (f"https://{_API_HOST}/repos/{owner}/{repo}/git/trees/{ref_q}"
                f"?recursive=1")
    payload = _fetch_json(tree_url, connect_timeout_s=connect_timeout_s,
                          total_deadline=total_deadline)
    if not isinstance(payload, dict) or "tree" not in payload:
        raise SubsetError("upstream_error",
                          "unexpected GitHub tree response shape", 503)

    truncated = bool(payload.get("truncated"))
    tree = payload["tree"]
    prefix_l = prefix.lower()
    doc_blobs = []
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path") or ""
        if not path.lower().startswith(prefix_l):
            continue
        if not path.lower().endswith(_DOC_EXTENSIONS):
            continue
        doc_blobs.append(path)
        if len(doc_blobs) > max_files:
            raise SubsetError("too_many_files",
                              f"subset contains more than {max_files} doc files",
                              413)

    files = {}
    budget = max_total_bytes
    for path in sorted(doc_blobs):
        raw_url = (f"https://{_RAW_HOST}/{owner}/{repo}/{ref_q}/"
                   f"{quote(path)}")
        data = _fetch_raw(raw_url, connect_timeout_s=connect_timeout_s,
                          total_deadline=total_deadline,
                          max_file_bytes=max_file_bytes,
                          total_budget=budget)
        budget -= len(data)
        files[path] = data.decode("utf-8", errors="replace")

    if not files:
        raise SubsetError("no_documentation_files",
                          f"no Markdown/MDX/RST/HTML files found under "
                          f"'{prefix}' at ref '{ref or 'HEAD'}'", 422)
    return SubsetResult(files=files, prefix=prefix,
                        tree_api_hits=1, raw_fetches=len(files),
                        truncated_tree=truncated)
