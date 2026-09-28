# Docrot Scan API

A production-ready, **stdlib-only** Python HTTP service that scans public
GitHub repositories for documentation link rot.

You give it a public GitHub repository. It fetches the repo as a tarball,
scans every Markdown / MDX / reStructuredText / HTML file for remote
`http(s)` links and images, checks each one with bounded concurrency, and
returns a machine-readable JSON report of what is broken, with file and line
provenance.

Built for agents and automation operators: clearly priced, honestly limited,
no hidden behavior.

## Version & history

The running service reports its own version on `GET /health` and in
`.well-known/agent-service.json` — those endpoints are the source of
truth, and this README deliberately pins no version. Release notes:
[Releases](https://github.com/auroraxo/docrot-api/releases).

## Pricing (manual invoicing / pilot)

> **This is manual invoicing for a pilot program. There is NO automatic
> billing, payment enforcement, or paywall in this version.**

| Item | Value |
|---|---|
| Price | **US$1.00 per completed scan** |
| First external pilot scan | **Free** |
| Payment | Payable **after** result delivery, in **SOL** |
| SOL address | `CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn` |
| Billing model | `manual-invoicing-pilot` — an operator issues the invoice manually |

Every successful scan response carries a machine-readable `receipt` object
including the billing block above. The SOL amount shown is a **reference
quote**; the USD price is the contractual price. Payment is on trust for now:
you receive the result first.

## Quick start

```bash
# run (no dependencies to install)
python3 -m docrot_scan_api

# or as a console script
pip install .
docrot-scan-api
```

Then:

```bash
# local development
curl -sS -X POST http://127.0.0.1:8087/v1/scan \
  -H 'Content-Type: application/json' \
  -d '{"repository":"https://github.com/owner/repo","ref":"main"}'
```

Production — one exact, copy-paste-ready call against the live base URL
`https://codebyaurora.com/docrot-api/`:

```bash
curl -sS -X POST https://codebyaurora.com/docrot-api/v1/scan \
  -H 'Content-Type: application/json' \
  -d '{"repository":"https://github.com/auroraxo/docrot-api","ref":"main"}'
```

Example response (abridged):

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
  "receipt": { "kind": "scan-completed", "billing": { "...": "..." } }
}
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/scan` | Run a link-rot scan |
| `GET` | `/health` | Liveness probe |
| `GET` | `/` | Service description |
| `GET` | `/.well-known/agent-service.json` | Machine-readable service descriptor |

Full request/response schema, error codes, and limits:
**[docs/API.md](docs/API.md)**.

## What it scans

- `*.md`, `*.markdown` — inline links, images, autolinks, reference definitions
- `*.mdx` — same as Markdown plus HTML `href`/`src` attributes
- `*.rst` — named/anonymous hyperlinks, `:ref:`/`:doc:` roles,
  `.. image::` / `.. figure::` directives, link targets, bare URLs
- `*.html`, `*.htm` — `href`, `src`, `<meta http-equiv="refresh">`

Only **remote** `http(s)` URLs are checked. `mailto:`, `ftp:`, relative
paths, and `#anchors` are ignored.

## Safety model (summary)

- Only `https://github.com/<owner>/<repo>` URLs are accepted — validated,
  never passed to a shell. The archive is downloaded over HTTPS from
  `codeload.github.com` with hard size/time caps.
- Extraction is in-memory with zip-slip, symlink, and decompression-bomb
  defenses.
- Every checked URL is DNS-vetted before connection: loopback, private
  (RFC1918), link-local, reserved, ULA, and IPv4-mapped addresses are
  refused (SSRF defense), and the connection is pinned to the vetted IP.
- Hard limits on request size, archive size, job duration, URL count, and
  concurrency. Details in docs/API.md.

## Configuration

All limits are environment-driven with safe defaults — see the table in
[docs/API.md](docs/API.md). The two you will actually set:

| Variable | Default | Purpose |
|---|---|---|
| `DOCROT_HOST` | `127.0.0.1` | Bind address (put nginx in front for TLS) |
| `DOCROT_PORT` | `8087` | Bind port |

## Deploy

See `deploy/docrot-scan-api.service` (systemd unit) and `deploy/nginx.conf`
(reverse proxy with TLS termination, rate limiting, body-size guard).
Production base URL: **https://codebyaurora.com/docrot-api/**
Source: **https://github.com/auroraxo/docrot-api**

## Development

```bash
python3 -m unittest discover -s tests -v
```

The test suite uses mocked network everywhere except one local integration
test that binds an ephemeral port on localhost; no external calls are made.

## License

MIT — see [LICENSE](LICENSE).
