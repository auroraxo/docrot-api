# Docrot Scan API — Issue #1

## Problem statement

Documentation rots. Markdown, MDX, reStructuredText, and HTML files in public
GitHub repositories accumulate links and image references that silently die —
domains expire, pages move, CDNs are withdrawn. Human link checkers do not scale
and are not machine-callable. Agents and automation operators need a small,
clearly priced, paid HTTP API that answers one question:

> "Given a public GitHub repository, which remote `http(s)` links and images in
> its documentation files are broken?"

The answer must be structured JSON suitable for machine consumption, with
honest errors, bounded resource usage, and predictable pricing. There is no
existing stdlib-only service in this repository that provides this.

## Non-goals (for this milestone)

- No automatic payment enforcement. Payment is **manual invoicing / pilot**:
  the first scan per customer is free, subsequent scans are billed manually in
  SOL after the result is delivered. The README and docs must say exactly this
  and must not pretend billing is automatic.
- No shelling out to `git`, `curl`, or any user-controlled input.
- No JavaScript rendering or following of redirects into infinite chains; the
  scan is a bounded static-link liveness check, not a browser.

## Proposed solution

A single-file-deployable, Python-stdlib-only HTTP service (`http.server`
based, no third-party packages):

1. `POST /v1/scan` accepts `{"repository": "https://github.com/owner/repo",
   "ref": "optional-ref"}`.
2. The repository reference is validated as a **public GitHub HTTPS URL**
   (owner/repo shape, no credentials, no file paths, no URLs of other hosts).
3. The repo is fetched **safely** as a tarball archive via GitHub's codeload
   endpoint (bounded by size and time limits), extracted in memory, with
   zip-slip / traversal / symlink defenses.
4. Markdown (`.md`, `.markdown`), MDX (`.mdx`), reStructuredText (`.rst`),
   and HTML (`.html`, `.htm`) files are scanned for remote `http(s)` links and
   images. Non-remote schemes (`mailto:`, `ftp:`, relative paths, `#anchors`)
   are ignored.
5. Collected URLs are checked with **bounded concurrency** (HEAD first, GET
   fallback on 405/501/403/429 when allowed), with per-request timeouts and
   a global job deadline.
6. Every resolved target IP is vetted against an SSRF blocklist
   (loopback, RFC1918 private, link-local, reserved, ULA, etc.). DNS
   re-resolution pinning is used so the connection goes to the vetted IP.
7. Response:

```json
{
  "repository": "https://github.com/owner/repo",
  "ref": "main",
  "scannedFiles": 12,
  "checkedUrls": 34,
  "broken": [
    {
      "url": "https://example.net/gone",
      "status": 404,
      "source": "docs/README.md",
      "line": 7,
      "error": "HTTP 404"
    }
  ],
  "durationMs": 4211,
  "requestId": "01J...",
  "receipt": {
    "kind": "scan-completed",
    "requestId": "01J...",
    "repository": "https://github.com/owner/repo",
    "ref": "main",
    "scannedFiles": 12,
    "checkedUrls": 34,
    "brokenCount": 1,
    "durationMs": 4211,
    "completedAt": "2026-09-12T22:04:00Z",
    "billing": {
      "model": "manual-invoicing-pilot",
      "currency": "SOL",
      "amountDueUsd": 1.0,
      "amountDue": "0.0065",
      "payTo": "CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn",
      "terms": "Payable after result delivery. First external pilot scan is free. No automatic billing in this version; an operator issues the invoice manually.",
      "pilotFree": false
    }
  }
}
```

(`amountDue` is a fixed reference quote; actual SOL/USD fluctuates. The USD
price of US$1 per completed scan is the contractual price.)

## Acceptance criteria

1. **Endpoints**
   - [ ] `GET /health` returns `200` with `{"status":"ok"}` and no auth.
   - [ ] `GET /` returns `200` with a human/machine-readable service
         description (service name, version, endpoints, pricing pointer).
   - [ ] `GET /.well-known/agent-service.json` returns the same
         machine-readable descriptor file served from disk.
   - [ ] `POST /v1/scan` implements the full scan flow and returns the
         documented success schema.
2. **Input validation**
   - [ ] Non-POST or wrong-content-type requests to `/v1/scan` get `4xx`.
   - [ ] Bodies larger than the request-size limit are rejected with `413`.
   - [ ] Malformed JSON, missing `repository`, wrong types, and extra
         unknown fields are rejected with `400` and a machine-readable
         `error.code`.
   - [ ] Only `https://github.com/<owner>/<repo>` (optionally with a GitHub
         UI ref path) is accepted. `http://`, other hosts, missing owner/repo,
         embedded credentials, trailing file paths, and non-GitHub hosts are
         rejected with `422`/`400`.
   - [ ] `ref`, when present, must match a conservative safe-character set
         and length bound (it is interpolated into an archive URL, never into
         a shell — but validate anyway).
3. **Fetch safety**
   - [ ] The tarball is downloaded over HTTPS with size and time caps; a too
         large archive yields an honest `413`-style job error, a timeout a
         `504`-style job error (both within the documented schema).
   - [ ] Extraction is memory-bounded per-file and rejects path traversal
         (`../`, absolute paths) and symlink/hardlink members.
   - [ ] No subprocess is ever spawned with user input.
4. **Scanning & checking**
   - [ ] Exactly `.md .markdown .mdx .rst .html .htm` files are scanned.
   - [ ] Markdown links/images, autolinks, bare `http(s)` links in RST
         bodies and inline/link directives, and `href`/`src` attributes in
         HTML are extracted with line numbers.
   - [ ] Only remote `http(s)` URLs are checked; `mailto:`, `ftp:`,
         relative, and anchor-only references are ignored.
   - [ ] Deduplicated URLs are checked with bounded concurrency
         (default 8 workers), a per-URL timeout, and a global deadline.
   - [ ] HEAD is tried first; on 405/501/403/429 a single GET fallback is
         made (with capped response reading).
   - [ ] `broken[]` entries carry `url`, `status`, `source`, `line`,
         `error` and preserve `source:line` provenance of every occurrence.
   - [ ] DNS resolution of checked hosts is vetted: loopback, private
         (RFC1918), link-local (169.254/16, fe80::/10), reserved,
         unspecified, ULA, and IPv4-mapped IPv6 addresses are rejected
         before any connection (SSRF defense).
5. **Limits & honesty**
   - [ ] Request body size, scan duration, per-file size, total archive
         size, URL count, and concurrency limits are enforced and documented
         in `docs/API.md`.
   - [ ] Over-limit jobs fail with honest job-level errors
         (`duration_exceeded`, `archive_too_large`, `too_many_urls`, ...)
         instead of timing out silently or hanging.
   - [ ] Every successful response includes a `receipt` object and
         `requestId`.
6. **Observability**
   - [ ] Structured JSONL access log (one line per HTTP request) and job log
         (one line per scan job) to stderr or files configured by env.
7. **Packaging & docs**
   - [ ] `pyproject.toml` present; project installs as `docrot_scan_api`;
         zero runtime dependencies outside the stdlib.
   - [ ] `README.md` documents pricing (US$1 per completed scan, first
         external pilot free, payable after result in SOL to the listed
         address) and explicitly labels payment **manual invoicing / pilot —
         no automatic billing**.
   - [ ] `docs/API.md` documents exact request/response schemas, all limits,
         pricing, a test plan, and the security model.
   - [ ] `deploy/docrot-scan-api.service` (systemd) and
         `deploy/nginx.conf` examples are provided.
   - [ ] `public/.well-known/agent-service.json` is served at
         `/.well-known/agent-service.json`.
   - [ ] `LICENSE` present (MIT).
8. **Tests**
   - [ ] `python3 -m unittest discover -s tests -v` passes with **all tests
         passing**, covering: input validation, limits, archive fetch
         (mocked), extraction safety (zip-slip, symlinks), link extraction
         per format, URL checking (mocked HTTP), SSRF rejection, receipt
         presence, health/root/well-known endpoints, JSONL logging.
   - [ ] At least one **local integration test** starts the real server on
         an ephemeral port and drives `GET /health`, `GET /`,
         `GET /.well-known/agent-service.json`, and `POST /v1/scan` with a
         mocked GitHub fetch, asserting the end-to-end response schema.
9. **Process**
   - [ ] Everything committed with
         `Signed-off-by: Aurora <aurora9c69543e@atomicmail.ai>`.
   - [ ] Real test results included in the closing report.

## Test plan

| # | Area | Case | Expected |
|---|------|------|----------|
| T1 | Validation | `http://github.com/o/r` | 422 `unsupported_repository_host` |
| T2 | Validation | `https://gitlab.com/o/r` | 422 `unsupported_repository_host` |
| T3 | Validation | `https://github.com/norepo` | 422 `invalid_repository` |
| T4 | Validation | `https://user:pw@github.com/o/r` | 422 `invalid_repository` |
| T5 | Validation | `https://github.com/o/r/tree/dev/docs` | 422 `invalid_repository` (paths not allowed) |
| T6 | Validation | ref `--upload-pack=evil` | 422 `invalid_ref` |
| T7 | Validation | body 2 MB | 413 `request_too_large` |
| T8 | Validation | malformed JSON / missing field / unknown field | 400 `invalid_json` / `missing_field` / `unknown_field` |
| T9 | Fetch | mocked codeload 200 tarball | files extracted, scan proceeds |
| T10 | Fetch | mocked codeload 404 | honest job error `repository_not_found` |
| T11 | Fetch | mocked oversized archive | `archive_too_large` |
| T12 | Extraction | tar with `../evil` member | member skipped, extraction safe |
| T13 | Extraction | symlink member | member skipped |
| T14 | Scan | MD/MDX/RST/HTML fixtures | correct urls + line numbers; non-http(s) ignored |
| T15 | Check | mocked 404 URL | appears in `broken[]` with status+source+line |
| T16 | Check | HEAD 405 → GET 200 | not broken |
| T17 | Check | URL resolving to 127.0.0.1 | rejected by SSRF guard, marked broken `ssrf_blocked` |
| T18 | Check | > max_urls distinct URLs | job error `too_many_urls` |
| T19 | Job | deadline exceeded (sleep injected) | job error `duration_exceeded` |
| T20 | Endpoints | `/health`, `/`, `/.well-known/agent-service.json` | 200 with documented bodies |
| T21 | Integration | real server on ephemeral port + mocked fetch | full end-to-end schema incl. receipt + requestId |
| T22 | Logging | one scan through server | valid JSONL lines in access+job logs |
