# Changelog

All notable changes to the **Docrot Scan API** (`docrot-scan-api`) are
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.7.0] — 2026-10-09

### Added
- **Direct purchase path (`POST /v1/checkout` + `GET /v1/checkout/{orderId}`)**:
  a customer who already received a verified scan result can now pay the
  US$1.00 without waiting for a manual invoice. Each order carries a unique
  Solana Pay `reference` (random 32-byte base58 address) and an exact SOL
  reference quote, so the payment is attributable on-chain — no accounts, no
  API keys, no webhooks. Payment detection queries a public Solana mainnet
  JSON-RPC endpoint (`DOCROT_SOLANA_RPC`, timeout `DOCROT_SOLANA_RPC_TIMEOUT_S`)
  and is rate-limited per order by `DOCROT_CHECKOUT_VERIFY_COOLDOWN_S`
  (default 10 s).
- **Honest degradation**: when the RPC is unreachable or rate-limited the
  order stays `pending` and the response carries
  `"verification": "unavailable"` — never a fabricated paid/pending verdict.
  Orders persist in `DOCROT_CHECKOUT_STORE` (defaults next to
  `DOCROT_JOB_LOG`) with atomic replace; an unwritable store falls back to
  memory and logs `checkout_store_error`.
- Order TTL `DOCROT_CHECKOUT_TTL_S` (default 24 h); expired unpaid orders
  report `status: "expired"`.
- Suite 128 -> **154** (26 new checkout tests: store persistence/pruning,
  order lifecycle, HTTP routes, RPC parsing/underpayment/transport failure).

## [1.6.0] — 2026-09-29

### Fixed
- **Parity with the open-source scanner v8 (docrot v1.6.0), closing the two
  phantom classes found during the scanner's v7 rescan verification:**
  fenced-code stripping now tolerates deep indentation (0-7 spaces — fences
  inside list items render as code on GitHub), and inline-code pairing is
  list-item-local (an odd backtick run in one list item no longer shifts
  the pairing of the next item's balanced span). Each defect was verified
  against GitHub's rendered HTML on real repositories before the fix.
- Line-number provenance unchanged; suite 126 -> **128**.

## [1.5.0] — 2026-09-29

### Fixed
- **Inline-code span pairing is CommonMark-accurate** (parity with the
  open-source scanner v7, docrot v1.5.1/v1.5.2): backtick runs pair by
  equal run length, paragraph-locally, instead of positionally. Found by
  manually verifying a scanner false positive on a real 68 KB doc whose
  code-span image example (rendered by GitHub as `<code>`) the API would
  likewise have live-checked into a false "broken" verdict. The
  paragraph-local rule also closes the triple-newline edge: a content
  part starting with a single newline is no longer misrouted as a
  blank-line separator. Line-number provenance is unchanged: blanked
  spans keep their newlines.

### Added
- 5 regression tests: phantom span, paragraph locality, triple-newline
  edge, real link between spans, interleaved backtick lengths; suite
  119 -> **124 green**.

---

## [1.4.1] — 2026-09-28

### Fixed
- **Root endpoint `docs` pointer is absolute now:** `GET /` returned
  `"docs": "docs/API.md"` — a relative path that resolves against the
  service base and 404s behind the public edge, breaking the discovery
  chain for machine consumers. It now returns the same absolute URL the
  agent descriptor carries (`https://github.com/auroraxo/docrot-api/blob/main/docs/API.md`).

### Added
- **Edge contract replay checks root discovery (17 → 18 checks):** the root
  `docs` pointer must be an absolute `https://` URL — closing the exact gap
  that let this drift ship. Regression assertion added to
  `test_root_describes_service` (suite stays 119 green).

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
