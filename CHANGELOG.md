# Changelog

All notable changes to the **Docrot Scan API** (`docrot-scan-api`) are
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---


## [1.12.1] - 2026-10-10

### Fixed
- **Subset scans on very large repos**: the git-tree API fetch read a fixed
  4 MB cap, so recursive trees larger than that (first seen live on
  `PostHog/posthog`) returned truncated JSON and a 503
  `unparseable upstream response` before the truncated-tree fallback could
  trigger. `_fetch_json` now reads in bounded chunks until EOF under the
  same total deadline (new `upstream_timeout` code on deadline overrun).
  Regression test `test_large_tree_payload_not_truncated_by_read_cap`
  covers a >4 MB tree payload served in chunks. Suite 178 -> 179 green.

## [1.12.0] — 2026-10-10

### Subset scans via `pathPrefix` (large-repo unblock)

Repositories whose full archive exceeds `DOCROT_MAX_ARCHIVE_BYTES` (50 MiB
default) can now be scanned without downloading the entire codeload tarball.
A request may carry `pathPrefix` (default `docs/`); the server walks the git
tree via the public GitHub API and fetches only the doc files under that
prefix from `raw.githubusercontent.com`. The same hard caps apply
(`maxArchiveBytes` as a total-raw-fetch budget, `maxFileBytes` per file,
`maxFiles` per job, `maxJobSeconds` total deadline). The full archive is
never downloaded. The response carries `"mode": "subset"` and echoes the
normalized `"pathPrefix"`; the `receipt` block is unchanged, so `POST
/v1/checkout` works the same way. Errors: `invalid_path_prefix` (422),
`repository_or_ref_not_found` (404), `too_many_files` (413),
`no_documentation_files` (422), `upstream_rate_limited` (429).

This unblocks GrowthBook (292 MiB archive > 50 MiB cap), which previously
failed the Distribution-Pivot Send 3 DoD-2 verified-receipt requirement.
## [1.11.0] — 2026-10-09

### Added
- **Self-serve browser scan form**: `GET /v1/scan-form` serves a
  self-contained HTML page (inline CSS + inline JS, no external assets,
  no third-party scripts) where a non-developer pastes a public GitHub
  URL, runs `POST /v1/scan` on the same origin, sees the receipt, and
  opens the self-serve payment page with one click. Closes the last
  funnel gap for a buyer who arrived via a link rather than the docs.
- The machine-readable descriptor (`/.well-known/agent-service.json`)
  and the root document now list `scanForm` alongside the other
  endpoints.

## [1.10.0] — 2026-10-09

### Added
- **Human payment page**: `GET /v1/checkout/{orderId}` now serves a small
  self-contained HTML page when the client sends `Accept: text/html` —
  amount, address, reference, Solana Pay deep link, auto-refresh while
  pending, paid/expired states. API clients that send `Accept:
  application/json` (or `*/*`, or no `Accept`) get the JSON payload
  unchanged; the page is rendered from the same `order_payload()` dict, so
  the two views can never disagree.

### Changed
- **Billing story synced with reality in README + docs/API.md**: the
  "invoice follows manually / nothing automatic" wording predates the
  self-serve checkout (1.7.0+) and read as a contradiction. Both documents
  now describe the actual funnel: scan first, receipt carries
  `billing.selfServe`, `POST /v1/checkout` creates the order, on-chain
  verification detects payment; manual invoicing remains the fallback.
  The `"Important: this version does not enforce or automate billing"`
  note now states precisely what is and is not automated (scan access is
  never gated; payment *detection* is on-chain; invoicing can still be
  manual).

---

## [1.9.0] — 2026-10-09

### Added
- **Server-side payment watcher**: a daemon-thread sweep re-checks every
  live pending order once per `DOCROT_CHECKOUT_WATCH_INTERVAL_S` (default
  60 s, `0` disables). A payer who sends the SOL and never opens
  `GET /v1/checkout/{orderId}` again is now still detected — before this,
  such an order stayed `pending` forever and the revenue went unnoticed.
  The watcher shares the per-order cooldown with status polls (one RPC
  budget) and degrades honestly: RPC outages keep orders `pending` and log
  `checkout_verify_error`; a sweep crash logs `checkout_watch_error` instead
  of dying silently. Startup logs `checkout_watch_started`.
- **`paidVia` on paid orders** (`status-poll` or `server-watch`): revenue
  records now say how the payment was found; also exposed in the order
  payload. `checkout_paid` journal events carry the same `via` field.
- `OrderStore.pending()` — snapshot of live, unpaid, unexpired orders.

---

## [1.8.0] — 2026-10-09

### Added
- **Self-serve block in every scan receipt**: `receipt.billing.selfServe`
  (`checkoutEndpoint: /v1/checkout`, `method: POST`, `description`) points the
  customer at the direct purchase path from the scan result itself, so the
  funnel no longer depends on the caller having read the README.

### Changed
- `receipt.billing.terms` and the `agent-service.json` pricing terms now state
  the self-serve option alongside manual invoicing instead of "No automatic
  billing in this version".

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
