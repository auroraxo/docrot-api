# Changelog

All notable changes to the **Docrot Scan API** (`docrot-scan-api`) are
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.4.0] — 2026-09-28

### Changed — Breaking HTTP Statuses (same `error.code`)
- **Remapped gateway-status errors to edge-safe codes:** Every error
  previously mapped to `502` or `504` now returns `404`, `403`, or `503`.
  The public edge (Cloudflare) was replacing origin `502`/`504` response
  bodies with its own 16-byte plain-text page (`error code: 502`), silently
  stripping the JSON error envelope in production. Clients branch on
  `error.code`, which is unchanged:
  - `repository_or_ref_not_found`: `502` → `404` (semantically true)
  - `repository_not_public`: `502` → `403`
  - `upstream_error`, `archive_unreadable`: `502` → `503`
  - `fetch_timeout`, `fetch_interrupted`, `upstream_unreachable`: `504` → `503`
  - `duration_exceeded`: `504` → `503`
  - `FetchError` default `http_status`: `502` → `503`
- **Documentation:** `docs/API.md` error and limits tables updated to `503`
  for all job-level deadline/timeout cases. New section *"Why no 502/504?"*
  documents the edge-probe findings.

### Added
- **Edge contract replay tool (`tools/edge_contract_replay.py`):** 17 checks
  replaying every documented error case, health, and agent descriptor against
  the public production edge or direct origin.
- **Client documentation:** Added guidance in `README.md` and `docs/API.md`
  noting that Cloudflare challenges the default Python `urllib` User-Agent
  (`Python-urllib/3.x` → `403` code 1010) while `requests`, `httpx`, `aiohttp`,
  Go, and Node clients pass through untouched.
- **Prefix reverse-proxy specification:** Reference proxy configuration in
  `deploy/nginx.conf` updated to whole-prefix routing and `error_page 413`
  mapping, guaranteeing JSON envelopes for unknown routes and oversized bodies.

---

## [1.3.0] — 2026-09-28

### Fixed
- **reStructuredText literal blocks:** URLs inside paragraphs ending in `::`
  and inside `.. code-block::` / `.. sourcecode::` directives are no longer
  live-checked. Directives that render content (`.. note::`, `.. seealso::`)
  continue to be checked.
- **HTML comments:** Links and images inside `<!-- ... -->` comments are
  stripped across Markdown, MDX, RST, and HTML extractors before checking.
- **Line-number provenance:** Both strippers blank lines with newline
  preservation, ensuring reported line numbers in findings match source files.

---

## [1.2.0] — 2026-09-28

### Fixed
- **Markdown code blocks and inline spans:** URLs inside fenced code blocks
  (```` ``` ```` / `~~~`) and inline backtick code spans are excluded from
  liveness checks. Example URLs no longer generate false broken verdicts.
- **CommonMark compliance:** Unclosed code fences correctly extend to the
  end of the file without crashing or leaking content.

---

## [1.1.0] — 2026-09-28

### Fixed
- **HTML entity decoding:** Extracted URLs are decoded (`html.unescape`)
  before dispatching HTTP checks (`&amp;`, `&#x3D;`, etc.). Mirrors the
  open-source scanner's v1.2.0 entity-handling fix.

---

## [1.0.0] — 2026-09-12

### Added
- Initial public release of the paid Docrot Scan API.
- Endpoints: `GET /`, `GET /health`, `POST /v1/scan`,
  `GET /.well-known/agent-service.json`, `GET /.well-known/security.txt`.
- Safe GitHub codeload archive extraction in memory (tar.gz), SSRF-protected
  outgoing image/link checker with DNS rebinding prevention, and machine-readable
  billing receipts (`manual-invoicing-pilot` model).
