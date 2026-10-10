"""HTTP server: routing, request parsing, honest error responses.

Threading stdlib HTTP server. No third-party dependencies.

Routes:
    GET  /health                        -> liveness
    GET  /                              -> human+machine service description
    GET  /.well-known/agent-service.json -> machine-readable descriptor from disk
    GET  /v1/scan-form                  -> self-contained browser scan form (HTML)
    POST /v1/scan                       -> run a scan job
    POST /v1/checkout                   -> create a direct-purchase order
    GET  /v1/checkout/{orderId}         -> order status (+ on-chain verify)
"""

import json
import re
import socketserver
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import checkout as checkout_mod
from . import github as github_mod
from . import subset as subset_mod
from .requestid import new_request_id
from .service import ScanService, JobError
from .version import SERVICE_NAME, VERSION

_MAX_BODY_BYTES_CAP = 1024 * 1024
_CHECKOUT_PATH = re.compile(r"^/v1/checkout/([A-Za-z0-9_]{10,64})$")


def _scan_form_html(service: str, version: str) -> str:
    """Self-contained browser scan form (1.11.0+; supports pathPrefix 1.12.0+).

    A non-developer who lands on ``GET /v1/scan-form`` (and any landing
    page that points at it) can paste a public GitHub URL and run a
    scan without reading the API docs. The same form already lives on
    ``https://codebyaurora.com/``; this route is the canonical machine
    endpoint, so a buyer who discovered the service from the docs
    descriptor can use it directly.

    The page is stdlib-only: no third-party JS, no external assets, no
    QR codes. A successful run surfaces the receipt with a one-click
    "Open payment page" that links to ``/v1/checkout/{orderId}`` —
    the human payment page (1.10.0+) renders the amount, address,
    reference, a deep-link to any Solana Pay wallet, and auto-refreshes
    while the order is pending.
    """
    from html import escape as esc
    title = f"{esc(service)} — scan a public GitHub repo"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 15px/1.55 system-ui, sans-serif; margin: 0; padding: 2rem 1rem;
       max-width: 40rem; margin-inline: auto; }}
h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
p.lede {{ margin: 0 0 1.25rem; opacity: .8; }}
form {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1rem; }}
input {{ flex: 1; min-width: 16rem; font: 14px ui-monospace, monospace;
         padding: .55rem .65rem; border: 1px solid #888; border-radius: 6px;
         background: transparent; color: inherit; }}
button {{ padding: .55rem 1rem; border: 0; border-radius: 6px;
          background: #2563eb; color: #fff; font-weight: 600; cursor: pointer; }}
button[disabled] {{ opacity: .6; cursor: progress; }}
.panel {{ border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
          border-radius: 8px; padding: .75rem 1rem; margin: 1rem 0; }}
.row {{ display: flex; gap: .75rem; justify-content: space-between;
        align-items: baseline; padding: .3rem 0; flex-wrap: wrap; }}
.row span {{ opacity: .7; }}
code {{ font: 13px/1.4 ui-monospace, monospace; word-break: break-all;
        text-align: right; }}
.broken {{ color: #b91c1c; }}
.broken li {{ margin: .15rem 0; font-family: ui-monospace, monospace;
              font-size: 13px; word-break: break-all; }}
.ok {{ color: #15803d; }}
.muted {{ opacity: .75; }}
.actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
.actions a {{ display: inline-block; padding: .45rem .85rem; border-radius: 6px;
              background: #2563eb; color: #fff; text-decoration: none;
              font-size: 13px; font-weight: 600; }}
.actions a.alt {{ background: #475569; }}
.status {{ font-weight: 600; margin: .25rem 0 1rem; }}
.status.idle {{ opacity: .7; }}
.status.error {{ color: #b91c1c; }}
.status.scanning {{ color: #b45309; }}
.status.complete {{ color: #15803d; }}
footer {{ margin-top: 1.5rem; font-size: 13px; opacity: .7; }}
</style>
</head>
<body>
<h1>{esc(service)}</h1>
<p class="lede">Paste any public GitHub repository URL. Result first; if a
<code>receipt</code> comes back the scan is billable at US$1.00 in SOL,
payable through the on-chain self-serve checkout. First external pilot scan
is free. Service version <code>{esc(version)}</code>.</p>
<form id="f" onsubmit="return runScan(event)">
  <input id="repo" type="text" required
         placeholder="https://github.com/&lt;owner&gt;/&lt;repo&gt;"
         value="https://github.com/auroraxo/aurora-node-auditor">
  <input id="ref" type="text" placeholder="ref" value="main" style="max-width: 8rem;">
  <button id="go" type="submit">Run free scan</button>
</form>
<p class="status idle" id="state">idle</p>
<div id="out"></div>
<footer>Machine-readable: <code>GET /.well-known/agent-service.json</code> ·
Full reference: <a href="https://github.com/auroraxo/docrot-api/blob/main/docs/API.md">docs/API.md</a>
· Hosted example: <a href="https://codebyaurora.com/#scan-live">codebyaurora.com</a></footer>
<script>
const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
async function runScan(ev) {{
  ev.preventDefault();
  const repo = document.getElementById('repo').value.trim();
  const ref = document.getElementById('ref').value.trim();
  const btn = document.getElementById('go');
  const state = document.getElementById('state');
  const out = document.getElementById('out');
  btn.disabled = true;
  state.className = 'status scanning';
  state.textContent = 'scanning…';
  out.innerHTML = '';
  try {{
    const res = await fetch('/v1/scan', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{repository: repo, ref: ref || 'main'}})
    }});
    const data = await res.json();
    if (data.error) {{
      state.className = 'status error';
      state.textContent = 'error: ' + data.error.code;
      out.innerHTML = '<p class="muted">' + esc(data.error.message) + '</p>';
      return false;
    }}
    const broken = data.broken || [];
    state.className = 'status complete';
    state.textContent = 'complete — ' + data.checkedUrls + ' URLs, '
      + broken.length + ' broken (' + data.durationMs + 'ms)';
    let html = '<div class="panel"><div class="row"><span>Repository</span>'
      + '<code>' + esc(data.repository) + (data.ref ? '@' + esc(data.ref) : '')
      + '</code></div>'
      + '<div class="row"><span>Files scanned</span><code>' + data.scannedFiles
      + '</code></div>'
      + '<div class="row"><span>Receipt</span><code>' + esc(data.requestId)
      + '</code></div></div>';
    if (broken.length) {{
      html += '<p>Broken links / dead images:</p><ul class="broken">';
      for (const b of broken) {{
        html += '<li>' + esc(b.url) + ' <span class="muted">('
          + esc(b.source) + ':' + b.line + ' · '
          + esc(b.status || b.error || '?') + ')</span></li>';
      }}
      html += '</ul>';
    }} else {{
      html += '<p class="ok">✓ No broken links or dead images found.</p>';
    }}
    if (data.receipt && data.receipt.billing) {{
      const b = data.receipt.billing;
      const pay = b.selfServe || {{}};
      html += '<div class="panel"><div class="row"><span>Billable</span>'
        + '<code>US$' + Number(b.amountDueUsd).toFixed(2) + ' ('
        + esc(b.amountDue) + ' SOL)</code></div>'
        + '<div class="row"><span>Pilot</span><code>'
        + (b.pilotFree ? 'first pilot scan free' : 'standard rate') + '</code></div>'
        + '<div class="row"><span>Self-serve</span><code>'
        + (pay.checkoutEndpoint ? esc(pay.checkoutEndpoint) : 'manual invoice')
        + '</code></div></div>';
      html += '<div class="actions">'
        + '<button id="pay" type="button">Pay US$1.00 for this scan</button>'
        + '<a class="alt" target="_blank" rel="noopener" '
        + 'href="https://github.com/auroraxo/docrot-api/blob/main/docs/API.md#post-v1checkout">API reference</a>'
        + '</div><div id="pay-out" style="margin-top:8px;"></div>';
    }}
    out.innerHTML = html;
    const payBtn = document.getElementById('pay');
    if (payBtn) payBtn.addEventListener('click', () => openCheckout(
      data.receipt.requestId, data.repository));
  }} catch (e) {{
    state.className = 'status error';
    state.textContent = 'request failed';
    out.innerHTML = '<p class="muted">' + esc(e.message) + '</p>';
  }} finally {{
    btn.disabled = false;
  }}
  return false;
}}
async function openCheckout(requestId, repository) {{
  const out = document.getElementById('pay-out');
  out.innerHTML = '<span class="muted">opening checkout…</span>';
  try {{
    const res = await fetch('/v1/checkout', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{requestId: requestId, repository: repository}})
    }});
    const data = await res.json();
    if (data.error) {{
      out.innerHTML = '<span class="broken">' + esc(data.error.message)
        + '</span>';
      return;
    }}
    const orderId = data.orderId;
    const payPage = '/v1/checkout/' + orderId;
    out.innerHTML = '<div class="panel"><div class="row"><span>Order</span>'
      + '<code>' + esc(orderId) + '</code></div>'
      + '<div class="row"><span>Amount</span><code>'
      + esc(data.payment.amount) + ' SOL (US$'
      + Number(data.payment.amountUsd).toFixed(2) + ')</code></div>'
      + '<div class="row"><span>Address</span><code>'
      + esc(data.payment.payTo) + '</code></div>'
      + '<div class="row"><span>Reference</span><code>'
      + esc(data.payment.reference) + '</code></div></div>'
      + '<div class="actions">'
      + '<a target="_blank" rel="noopener" href="' + payPage + '">Open payment page</a>'
      + (data.payment.solanaPayUri
        ? '<a class="alt" target="_blank" rel="noopener" href="'
          + esc(data.payment.solanaPayUri) + '">Open in wallet (Solana Pay)</a>'
        : '')
      + '</div><p class="muted">The payment page auto-refreshes while the order is pending and flips to <code>paid</code> once the Solana chain confirms it. A server-side watcher re-checks every 60 s, so a payment is never claimed before it is seen on-chain.</p>';
  }} catch (e) {{
    out.innerHTML = '<span class="broken">' + esc(e.message) + '</span>';
  }}
}}
</script>
</body>
</html>
"""


class _BodyTooLarge(Exception):
    pass


class _BadRequest(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class DocrotHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVICE_NAME}/{VERSION}"
    protocol_version = "HTTP/1.1"

    # injected by make_server():
    config = None
    service = None
    access_logger = None
    wellknown_body = None
    checkout_store = None
    checkout_verifier = None

    # ------------------------------------------------------------------ util

    def _send_json(self, status: int, payload: dict, request_id=None):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", request_id or "n/a")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, status: int, html: str, request_id=None):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", request_id or "n/a")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _wants_html(self) -> bool:
        """Content negotiation for order status: HTML only if asked for.

        Browsers send ``Accept: text/html,...``; API clients send
        ``application/json`` or ``*/*``. The default (no/any Accept) stays
        JSON so every existing client contract holds unchanged.
        """
        accept = (self.headers.get("Accept") or "").lower()
        if "application/json" in accept:
            return False
        return "text/html" in accept

    def _access(self, status: int, duration_ms: int, request_id=None, **extra):
        if self.access_logger:
            self.access_logger.access(
                method=self.command,
                path=self.path.split("?")[0],
                status=status,
                durationMs=duration_ms,
                requestId=request_id,
                remoteAddr=self.client_address[0] if self.client_address else None,
                userAgent=self.headers.get("User-Agent"),
                **extra,
            )

    # ----------------------------------------------------------------- verbs

    def do_GET(self):
        started = time.monotonic()
        path = self.path.split("?")[0]
        request_id = new_request_id()
        try:
            if path == "/health":
                self._send_json(200, {"status": "ok",
                                      "service": SERVICE_NAME,
                                      "version": VERSION},
                                request_id)
                status = 200
            elif path == "/":
                self._send_json(200, self._root_document(), request_id)
                status = 200
            elif path == "/.well-known/agent-service.json":
                if self.wellknown_body:
                    self._send_json(200, self.wellknown_body, request_id)
                    status = 200
                else:
                    self._send_json(404, _error("not_found",
                                                "descriptor not deployed"),
                                    request_id)
                    status = 404
            elif path == "/v1/scan-form":
                # Self-contained browser scan form (1.11.0+; supports pathPrefix 1.12.0+). Stdlib-only,
                # no scripts that require a build, no external assets. The
                # one POST it makes is /v1/scan, on the same origin.
                self._send_html(200, _scan_form_html(SERVICE_NAME, VERSION),
                                request_id)
                status = 200
            elif path == "/v1/scan":
                self._send_json(405, _error("method_not_allowed",
                                            "use POST for /v1/scan"),
                                request_id)
                status = 405
            elif path == "/v1/checkout":
                self._send_json(405, _error("method_not_allowed",
                                            "use POST for /v1/checkout"),
                                request_id)
                status = 405
            elif path.startswith("/v1/checkout/"):
                status = self._handle_checkout_get(path, request_id)
            else:
                self._send_json(404, _error("not_found",
                                            f"no route for {path}"),
                                request_id)
                status = 404
            self._access(status, int((time.monotonic() - started) * 1000),
                         request_id)
        except (BrokenPipeError, ConnectionResetError):
            self._access(499, int((time.monotonic() - started) * 1000), request_id,
                         note="client disconnected")

    def do_POST(self):
        started = time.monotonic()
        path = self.path.split("?")[0]
        request_id = new_request_id()
        status = 500
        try:
            if path == "/v1/scan":
                status = self._handle_scan(request_id)
            elif path == "/v1/checkout":
                status = self._handle_checkout_post(request_id)
            else:
                self._send_json(404, _error("not_found",
                                            f"no route for {path}"), request_id)
                status = 404
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except Exception as exc:  # last-resort honest 500
            self._send_json(500, _error("internal_error", str(exc)), request_id)
            status = 500
        finally:
            self._access(status, int((time.monotonic() - started) * 1000),
                         request_id)

    # ------------------------------------------------------------- /v1/scan

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise _BadRequest("invalid_content_length",
                              "Content-Length header is not an integer")
        if length > self.config.max_request_bytes:
            raise _BodyTooLarge()
        return self.rfile.read(length)

    def _handle_scan(self, request_id: str) -> int:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            self._send_json(415, _error("unsupported_media_type",
                                        "Content-Type must be application/json"),
                            request_id)
            return 415

        try:
            body = self._read_body()
        except _BodyTooLarge:
            # consume nothing; just close politely after responding
            self.close_connection = True
            self._send_json(
                413,
                _error("request_too_large",
                       f"request body exceeds {self.config.max_request_bytes} bytes"),
                request_id,
            )
            return 413

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, _error("invalid_json",
                                        f"request body is not valid JSON: {exc}"),
                            request_id)
            return 400

        if not isinstance(payload, dict):
            self._send_json(400, _error("invalid_json",
                                        "request body must be a JSON object"),
                            request_id)
            return 400

        allowed = {"repository", "ref", "pathPrefix"}
        unknown = set(payload) - allowed
        if unknown:
            self._send_json(400, _error("unknown_field",
                                        f"unknown fields: {sorted(unknown)}"),
                            request_id)
            return 400
        if "repository" not in payload:
            self._send_json(400, _error("missing_field",
                                        "missing required field: repository"),
                            request_id)
            return 400
        if "ref" in payload and payload["ref"] is not None and \
                not isinstance(payload["ref"], str):
            self._send_json(400, _error("invalid_field",
                                        "ref must be a string or null"),
                            request_id)
            return 400
        path_prefix = None
        if "pathPrefix" in payload:
            if payload["pathPrefix"] is not None and \
                    not isinstance(payload["pathPrefix"], str):
                self._send_json(400, _error("invalid_field",
                                            "pathPrefix must be a string or null"),
                                request_id)
                return 400
            try:
                path_prefix = subset_mod.normalize_prefix(payload["pathPrefix"])
            except subset_mod.SubsetError as exc:
                self._send_json(422, _error(exc.code, exc.message), request_id)
                return 422

        try:
            parsed = github_mod.normalize_repository_url(
                payload["repository"], github_host=self.config.github_host)
            if payload.get("ref"):
                github_mod.validate_ref(payload["ref"])
                parsed["ref"] = payload["ref"]
            if path_prefix is not None:
                parsed["pathPrefix"] = path_prefix
        except github_mod.RepositoryValidationError as exc:
            status = 422 if exc.code.startswith(("unsupported_",)) else 422
            self._send_json(status, _error(exc.code, exc.message), request_id)
            return status

        try:
            result = self.service.run_scan(parsed, request_id)
        except JobError as exc:
            self._send_json(exc.http_status, _error(exc.code, exc.message),
                            request_id)
            return exc.http_status

        self._send_json(200, result, request_id)
        return 200

    # ---------------------------------------------------------- /v1/checkout

    def _checkout_ready(self, request_id):
        if self.checkout_store is None or self.checkout_verifier is None:
            self._send_json(503, _error("checkout_unavailable",
                                        "checkout is not configured"), request_id)
            return False
        return True

    def _handle_checkout_post(self, request_id: str) -> int:
        if not self._checkout_ready(request_id):
            return 503
        content_type = (self.headers.get("Content-Type") or ";").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            self._send_json(415, _error("unsupported_media_type",
                                        "Content-Type must be application/json"),
                            request_id)
            return 415
        try:
            body = self._read_body()
        except _BodyTooLarge:
            self.close_connection = True
            self._send_json(413, _error("request_too_large",
                                        f"request body exceeds {self.config.max_request_bytes} bytes"),
                            request_id)
            return 413
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, _error("invalid_json",
                                        f"request body is not valid JSON: {exc}"),
                            request_id)
            return 400
        if not isinstance(payload, dict):
            self._send_json(400, _error("invalid_json",
                                        "request body must be a JSON object"),
                            request_id)
            return 400

        allowed = {"repository", "ref", "requestId"}
        unknown = set(payload) - allowed
        if unknown:
            self._send_json(400, _error("unknown_field",
                                        f"unknown fields: {sorted(unknown)}"),
                            request_id)
            return 400
        if "repository" not in payload:
            self._send_json(400, _error("missing_field",
                                        "missing required field: repository"),
                            request_id)
            return 400
        if "ref" in payload and payload["ref"] is not None and \
                not isinstance(payload["ref"], str):
            self._send_json(400, _error("invalid_field",
                                        "ref must be a string or null"),
                            request_id)
            return 400
        receipt_id = payload.get("requestId")
        if receipt_id is not None:
            if not isinstance(receipt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", receipt_id):
                self._send_json(400, _error("invalid_field",
                                            "requestId must be a short "
                                            "identifier (A-Z a-z 0-9 _ -, "
                                            "max 64 chars)"),
                                request_id)
                return 400

        try:
            parsed = github_mod.normalize_repository_url(
                payload["repository"], github_host=self.config.github_host)
            if payload.get("ref"):
                github_mod.validate_ref(payload["ref"])
                parsed["ref"] = payload["ref"]
        except github_mod.RepositoryValidationError as exc:
            self._send_json(422, _error(exc.code, exc.message), request_id)
            return 422

        order = checkout_mod.build_order(self.config, parsed["url"],
                                         parsed["ref"], receipt_id)
        self.checkout_store.create(order)
        doc = checkout_mod.order_payload(order, self.config,
                                         verification="not_checked")
        doc["repository"] = parsed["url"]
        self._send_json(201, doc, request_id)
        return 201

    def _handle_checkout_get(self, path: str, request_id: str) -> int:
        if not self._checkout_ready(request_id):
            return 503
        match = _CHECKOUT_PATH.fullmatch(path)
        if not match:
            self._send_json(404, _error("not_found",
                                        f"no route for {path}"), request_id)
            return 404
        order = self.checkout_store.get(match.group(1))
        if order is None:
            self._send_json(404, _error("order_not_found",
                                        "no checkout order with this orderId"),
                            request_id)
            return 404

        now = time.time()
        if order.get("status") != "paid" and order.get("expiresAt", 0) < now:
            verification = "skipped"
        else:
            verification = checkout_mod.check_payment(
                order, self.checkout_store, self.checkout_verifier,
                now=now,
                cooldown_s=self.config.checkout_verify_cooldown_s,
                source="status-poll")
            order = self.checkout_store.get(order["orderId"]) or order

        flag = {"verified": "verified", "pending": "pending",
                "unavailable": "unavailable",
                "skipped": ("verified" if order.get("status") == "paid"
                            else "expired" if order.get("expiresAt", 0) < now
                            else "cooldown")}[verification]
        doc = checkout_mod.order_payload(order, self.config, verification=flag)
        if order.get("expiresAt", 0) < now and order.get("status") != "paid":
            doc["status"] = "expired"
        if self._wants_html():
            self._send_html(200, checkout_mod.order_page_html(doc), request_id)
        else:
            self._send_json(200, doc, request_id)
        return 200

    # ------------------------------------------------------------------ docs

    def _root_document(self) -> dict:
        cfg = self.config
        return {
            "service": SERVICE_NAME,
            "version": VERSION,
            "description": "Paid link-rot scanning API for public GitHub "
                           "repositories. US$1 per completed scan; first "
                           "external pilot scan free; manual invoicing in SOL "
                           "after result delivery (no automatic billing).",
            "endpoints": {
                "health": "GET /health",
                "scan": "POST /v1/scan",
                "scanForm": "GET /v1/scan-form",
                "checkout": "POST /v1/checkout",
                "checkoutStatus": "GET /v1/checkout/{orderId}",
                "descriptor": "GET /.well-known/agent-service.json",
            },
            "scanRequest": {
                "repository": "https://github.com/<owner>/<repo> (required, "
                              "public GitHub HTTPS URLs only)",
                "ref": "optional git ref (branch/tag/commit-ish)",
            },
            "limits": {
                "maxRequestBytes": cfg.max_request_bytes,
                "maxJobSeconds": cfg.max_job_seconds,
                "maxArchiveBytes": cfg.max_archive_bytes,
                "maxFileBytes": cfg.max_file_bytes,
                "maxUrls": cfg.max_urls,
                "checkConcurrency": cfg.check_concurrency,
            },
            "pricing": {
                "amountUsd": cfg.price_usd_per_scan,
                "currency": "SOL",
                "referenceQuote": cfg.sol_reference_quote,
                "payTo": cfg.sol_pay_to,
                "billingModel": cfg.billing_model,
                "terms": "Payable after result delivery; first external pilot "
                         "scan free; invoiced manually - NOT automatic.",
            },
            "docs": "https://github.com/auroraxo/docrot-api/blob/main/docs/API.md",
        }

    # --------------------------------------------------------------- noise

    def log_message(self, format, *args):  # silence default stderr chatter
        return

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except TimeoutError:
            self.close_connection = True


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, config, service, access_logger,
                 wellknown_body, checkout_store=None, checkout_verifier=None):
        self.config = config
        self.service = service
        self.access_logger = access_logger
        self.wellknown_body = wellknown_body
        self.checkout_store = checkout_store
        self.checkout_verifier = checkout_verifier
        timeout = getattr(config, "request_timeout_s", 30)
        self.timeout = timeout
        super().__init__(address, handler)
        self.RequestHandlerClass.timeout = timeout
        self.RequestHandlerClass.config = config
        self.RequestHandlerClass.service = service
        self.RequestHandlerClass.access_logger = access_logger
        self.RequestHandlerClass.wellknown_body = wellknown_body
        self.RequestHandlerClass.checkout_store = checkout_store
        self.RequestHandlerClass.checkout_verifier = checkout_verifier

    def handle_error(self, request, client_address):
        # quiet, structured: noisy tracebacks from clients that hang up
        # mid-request are normal in production.
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(config, service: ScanService, access_logger=None,
                wellknown_body=None, checkout_store=None,
                checkout_verifier=None) -> _Server:
    """Create (not start) the HTTP server bound to config.host:config.port."""
    if access_logger is None:
        from .jsonl import make_access_logger
        access_logger = make_access_logger(config)
    return _Server((config.host, config.port), DocrotHandler, config, service,
                   access_logger, wellknown_body, checkout_store,
                   checkout_verifier)
