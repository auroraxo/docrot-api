# Docrot Scan API — API Reference

Version: 1.4.0
Base URL: `https://codebyaurora.com/docrot-api/` (production; the app itself
binds `http://127.0.0.1:8087` and HTTPS/PATH prefix terminates at the proxy —
see `deploy/nginx.conf`)

Production example:

```bash
curl -sS -X POST https://codebyaurora.com/docrot-api/v1/scan \
  -H 'Content-Type: application/json' \
  -d '{"repository":"https://github.com/auroraxo/docrot-api","ref":"main"}'
```

All responses are `application/json; charset=utf-8`. Every response carries an
`X-Request-Id` header, and its value is echoed in `requestId` on scan responses.

---

## POST /v1/scan

Scan a public GitHub repository for broken remote `http(s)` links and images
in Markdown/MDX/reStructuredText/HTML files.

### Request

```
POST /v1/scan
Content-Type: application/json

{
  "repository": "https://github.com/owner/repo",
  "ref": "main"                    // optional
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `repository` | string | yes | Must be `https://github.com/<owner>/<repo>`, optionally with a `/tree/<ref>` or `/blob/<ref>` suffix. `http://`, other hosts, credentials in the URL, query strings, fragments, and file paths deeper than `tree|blob/<ref>` are rejected. |
| `ref` | string \| null | no | Overrides any ref from the URL. Max 200 chars; allowed characters `[A-Za-z0-9._/-+]`; must not start with `.`, contain `..`, `//`, `%`, `\`, or use the reserved forms `refs/*`, `head`, `heads`, `tags`. |

### Success response — `200`

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
  "requestId": "01JDMQ8Z6X4WB3K2F7A9C1E5T8",
  "receipt": {
    "kind": "scan-completed",
    "requestId": "01JDMQ8Z6X4WB3K2F7A9C1E5T8",
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

| Field | Type | Meaning |
|---|---|---|
| `repository` | string | Canonical repository URL used |
| `ref` | string \| null | Ref used for the archive (`null` = default branch) |
| `scannedFiles` | integer | Number of doc files actually scanned |
| `checkedUrls` | integer | Number of **distinct** remote URLs checked |
| `broken[]` | array | Broken or unreachable URLs (see below) |
| `broken[].url` | string | The URL that failed |
| `broken[].status` | integer \| null | Last HTTP status if any (`null` when no response, e.g. DNS/timeout/SSRF-blocked; SSRF blocks surface as `403` with `error` starting `ssrf_blocked:`) |
| `broken[].source` | string | Repo-relative path of the referencing file |
| `broken[].line` | integer | 1-based line number in `source` |
| `broken[].error` | string \| null | Human-readable reason (`HTTP 404`, `timeout`, `ssrf_blocked: ...`, `too_many_redirects`, ...) |
| `durationMs` | integer | Wall-clock duration of the job |
| `requestId` | string | Unique job id (also on `X-Request-Id`) |
| `receipt` | object | Billing receipt, see [Pricing](#pricing) |

Every occurrence of a broken URL is listed separately (same `url`, different
`source`/`line`).

Semantics of "broken": HTTP status >= 400, redirect loop / too many
redirects, scheme downgrade redirect, DNS failure, connection refused /
unreachable, timeout, or SSRF-blocked address.

### Error responses

All errors share one shape:

```json
{ "error": { "code": "machine_readable_code", "message": "human readable" } }
```

| HTTP | `error.code` | When |
|---|---|---|
| 400 | `invalid_json` | Body is not valid JSON or not an object |
| 400 | `missing_field` | `repository` missing |
| 400 | `unknown_field` | Fields other than `repository`/`ref` present |
| 400 | `invalid_field` | `ref` present but not a string/null |
| 400 | `invalid_content_length` | Malformed `Content-Length` |
| 413 | `request_too_large` | Body over `DOCROT_MAX_REQUEST_BYTES` |
| 415 | `unsupported_media_type` | `Content-Type` present but not `application/json` |
| 422 | `unsupported_repository_scheme` | Not `https://` |
| 422 | `unsupported_repository_host` | Not `github.com` |
| 422 | `invalid_repository` | Bad owner/repo shape, credentials, query/fragment, disallowed path suffix |
| 422 | `invalid_ref` | Ref fails the safe-character/shape rules |
| 422 | `no_documentation_files` | Archive contained no scannable doc files |
| 429 | `upstream_rate_limited` | GitHub codeload returned 429 |
| 404 | `repository_or_ref_not_found` | codeload returned 404/410 |
| 403 | `repository_not_public` | codeload returned 401/403 |
| 503 | `upstream_error` / `archive_unreadable` | Other upstream failure or bad archive |
| 503 | `fetch_timeout` / `fetch_interrupted` / `upstream_unreachable` | Archive download failed/timed out |
| 503 | `duration_exceeded` | Job exceeded `DOCROT_MAX_JOB_SECONDS` |
| 413 | `archive_too_large` | Archive over `DOCROT_MAX_ARCHIVE_BYTES` |
| 413 | `too_many_files` | Archive contains more than `DOCROT_MAX_FILES` entries |
| 413 | `too_many_urls` | More than `DOCROT_MAX_URLS` distinct URLs found |
| 404 | `not_found` | Unknown route |
| 405 | `method_not_allowed` | e.g. `GET /v1/scan` |
| 500 | `internal_error` | Unexpected server fault (honest, never hidden) |

**Why no `502`/`504`?** This deployment’s public edge sits behind Cloudflare,
which replaces origin `502`/`504` response bodies with a 16-byte plain-text error
page — the machine-readable JSON would be lost for exactly the upstream-failure
codes that matter most. Verified by edge probe: `403`, `404`, `424`, `429`, `500`
and `503` pass through byte-intact; `502` and `504` do not. The codes above were
chosen to survive that edge while `error.code` remains the precise contract
(remapped from 502/504 in v1.4.0).

Note: job-level errors (`archive_too_large`, `duration_exceeded`, ...) are
**honest failures** — the job ran and was aborted for the stated reason; the
request is still billed as a completed scan only when a `200` with `receipt`
is returned.

---

## GET /health

```json
{ "status": "ok", "service": "docrot-scan-api", "version": "1.4.0" }
```

`200` always (unless the process is down). No auth, safe for load balancers.

---

## GET /

Service description: name, version, endpoint list, limits, pricing pointer,
and a summary of the scan request shape. Intended for both humans and
machine discovery.

---

## GET /.well-known/agent-service.json

Machine-readable service descriptor (served from
`public/.well-known/agent-service.json`): endpoints, JSON schemas for the
scan request/response, pricing block, limits, and capabilities. Agents can
use this for zero-context onboarding.

## GET /.well-known/security.txt

Machine-readable security contact file (`public/.well-known/security.txt`,
[RFC 9116](https://www.rfc-editor.org/rfc/rfc9116.html)): private reporting
links, acknowledgement goal, expiry. Served from two public paths:

1. `https://codebyaurora.com/.well-known/security.txt` — canonical RFC 9116
   path; host step: the file is copied to the web root
   (`/var/www/html/.well-known/security.txt`).
2. `https://codebyaurora.com/docrot-api/.well-known/security.txt` —
   service-prefixed path; nginx `alias` block next to the
   `agent-service.json` location.

**Deploy note:** `rsync public/` into the service tree covers the app copy
only. The two public paths are host-side (web-root copy + nginx alias) and
must be kept in place across host rebuilds. After any host change, verify
both return 200 with byte-identical content to
`public/.well-known/security.txt` — a deployed security file that answers
404 is exactly the rot class this project exists to eliminate.

---

## Limits

| Limit | Env var | Default | Behavior when exceeded |
|---|---|---|---|
| Max request body | `DOCROT_MAX_REQUEST_BYTES` | 65536 (64 KiB) | `413 request_too_large` |
| Max job wall time | `DOCROT_MAX_JOB_SECONDS` | 240 | `504 duration_exceeded` |
| Max archive size | `DOCROT_MAX_ARCHIVE_BYTES` | 52428800 (50 MiB) | `413 archive_too_large` |
| Max extracted file size | `DOCROT_MAX_FILE_BYTES` | 2097152 (2 MiB) | file skipped |
| Max archive entries | `DOCROT_MAX_FILES` | 20000 | `413 too_many_files` |
| Max distinct URLs per job | `DOCROT_MAX_URLS` | 1500 | `413 too_many_urls` |
| URL check concurrency | `DOCROT_CHECK_CONCURRENCY` | 8 | bounded, never exceeded |
| Per-request check timeout | `DOCROT_CHECK_TIMEOUT_S` | 10 | `timeout` error for that URL |
| Max redirects per URL | `DOCROT_CHECK_MAX_REDIRECTS` | 5 | `too_many_redirects` |
| Max bytes read per response | `DOCROT_CHECK_MAX_READ_BYTES` | 65536 | hard cap, connection closed |
| Fetch connect timeout | `DOCROT_FETCH_CONNECT_TIMEOUT_S` | 10 | `504` |
| Fetch total timeout | `DOCROT_FETCH_TIMEOUT_S` | 60 | `504` |

Other env vars: `DOCROT_HOST`, `DOCROT_PORT`, `DOCROT_GITHUB_HOST`,
`DOCROT_CODELOAD_HOST`, `DOCROT_ACCESS_LOG`, `DOCROT_JOB_LOG`,
`DOCROT_CHECK_USER_AGENT`, `DOCROT_LOG_LEVEL`, `DOCROT_WELLKNOWN_PATH`.
All numeric env values are clamped to safe minimums/maximums at startup.

## Pricing

| Item | Value |
|---|---|
| Price | **US$1.00 per completed scan** (a `200` response with `receipt`) |
| First external pilot scan | **Free** |
| Payment timing | **After** result delivery |
| Currency | **SOL** |
| Pay to | `CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn` |
| Billing model | `manual-invoicing-pilot` |

**Changed in 1.4.0 (breaking HTTP statuses):** every error previously mapped
to `502`/`504` now returns `404`, `403`, or `503` (table below) — the public edge
(Cloudflare) replaces origin `502`/`504` response bodies with its own plain-text
error page, silently stripping the JSON contract for exactly the upstream-failure
cases. `error.code` values are unchanged; **branch on `error.code`, not HTTP
status**. See "Why no 502/504?" below.

**Changed in 1.3.0:** URLs inside reStructuredText literal blocks (`::`
paragraph intro, `code-block`/`sourcecode` directives) and inside HTML
comments (`<!-- ... -->`) are no longer live-checked — neither ever renders
a link (third instance of the false-positive class fixed in v1.1.0/v1.2.0).
Rendered directives (`.. note::`, `.. seealso::`) keep their links; directive
lines carrying arguments (`.. image:: x.png`) never trigger blanking.
Line-number provenance stays exact. See Known limitations for the
deliberately unchecked cases.

**Changed in 1.2.0:** URLs inside fenced code blocks and inline code
spans are no longer checked in Markdown/MDX files — code examples are
not rendered, so live-checking them produced false "broken" verdicts
(same false-positive class as v1.1.0; mirrors the open-source scanner’s
``strip_code``; line-number provenance is preserved exactly; reStructuredText
is unaffected because backticks there are ordinary link syntax).

**Changed in 1.1.0:** HTML entities in extracted URLs (`&amp;`, `&#x3D;`, ...)
are now decoded before the liveness check, mirroring browser behavior —
raw entity literals no longer produce false "broken" verdicts (the same
false-positive class the open-source scanner fixed in v1.2.0; found here
by cross-checking the paid path against scanner v5, fixed with 5
regression tests).

**Important:** this version does **not** enforce or automate billing. There
is no paywall, no API keys, no on-chain verification. An operator reviews
job logs and invoices customers manually. The `receipt.billing` block exists
so agents can display/record the amount owed transparently. `amountDue` is a
reference SOL quote captured at deploy time; the USD price is contractual.

## Security model

1. **Input surface.** One POST route. `repository` must be a strict
   `https://github.com/<owner>/<repo>` URL (see validation rules above); `ref`
   is restricted to a safe character set and length. Everything else about the
   request is fixed.
2. **No shell, ever.** No `subprocess`/`os.system` anywhere in the codebase;
   user input never reaches a command line. The repo is fetched as a tarball
   from `codeload.github.com` over TLS (>= 1.2) with size/time caps.
3. **Archive safety.** Extraction is fully in-memory. Members that are not
   regular files (symlinks, hardlinks, devices) are skipped. Names with `..`
   segments, absolute paths, or Windows drive prefixes are skipped
   (zip-slip). Per-file and total size/count caps prevent decompression
   bombs. Text is decoded with `errors="replace"`.
4. **SSRF defenses on checked URLs.** Before any connection, the hostname is
   resolved and every resolved address is vetted: loopback, RFC1918 private,
   link-local (v4 + fe80::/10), reserved, multicast, unspecified, ULA
   (fc00::/7), IPv4-mapped IPv6, 6to4, and Teredo are refused. Non-standard
   ports are refused. Connections are pinned to the vetted IP (DNS cannot
   silently change between vetting and connect); redirects are followed
   manually and every hop is re-vetted.
5. **Bounded work.** Every loop has a cap (files, URLs, redirects, bytes,
   seconds, concurrency). A scan cannot run away.
6. **Honest errors.** Failures are structured and truthful — the API never
   reports a scan that did not happen, and never hides why it aborted.
7. **Logging.** Structured JSONL access and job logs (one JSON object per
   line) go to stderr and optionally to files (`DOCROT_ACCESS_LOG`,
   `DOCROT_JOB_LOG`). Logs never contain request bodies or repository
   contents; they contain ids, paths, statuses, and durations.
8. **Deployment posture.** Bind to `127.0.0.1` by default; terminate TLS and
   apply rate limiting at the nginx layer (see `deploy/`). The service runs
   as a dedicated unprivileged system user (see the systemd unit).

## Test plan

Run everything:

```bash
python3 -m unittest discover -s tests -v
```

Coverage matrix (mapped to ISSUE.md):

| Area | Tests |
|---|---|
| Repository URL validation | schemes, hosts, shapes, credentials, path suffixes, reserved names |
| Ref validation | charset, length, traversal, reserved forms |
| Request validation | JSON parse, missing/unknown/typed fields, content-type, body size |
| Archive fetch | mocked codeload: 200/404/403/429/oversize/timeout |
| Extraction safety | zip-slip members, symlink/hardlink members, bombs, extension filter |
| Link extraction | MD inline/image/autolink/ref-def, MDX HTML attrs, RST roles/directives/targets/bare, HTML href/src/meta-refresh; line numbers; non-http(s) exclusion |
| URL checking | mocked HTTP: 404 broken, HEAD-405→GET-200 ok, redirects, timeouts, SSRF blocks (loopback/private/link-local/reserved/mapped) |
| Limits | too many URLs, job deadline |
| Receipt & logs | receipt presence/shape, JSONL access+job log validity |
| Integration | real server on ephemeral port, full end-to-end flow with mocked GitHub fetch |

All network in unit tests is mocked (`unittest.mock`); the only real sockets
are loopback connections inside the integration test.

---

## Known limitations

- **Indented (4-space) Markdown code blocks are still live-checked.** A list
  item's continuation line is visually identical to an indented code block;
  stripping would hide real broken links (false negatives) — the worse
  failure for a paid check. Same reasoning for RST comment bodies (`..` +
  indented text), which cannot be distinguished from arbitrary directives
  without a full parser. Fenced blocks (```/~~~), inline spans, RST literal
  blocks (v1.3.0) and HTML comments (v1.3.0) are excluded.
