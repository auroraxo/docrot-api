"""HTTP server: routing, request parsing, honest error responses.

Threading stdlib HTTP server. No third-party dependencies.

Routes:
    GET  /health                        -> liveness
    GET  /                              -> human+machine service description
    GET  /.well-known/agent-service.json -> machine-readable descriptor from disk
    POST /v1/scan                       -> run a scan job
"""

import json
import re
import socketserver
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import github as github_mod
from .requestid import new_request_id
from .service import ScanService, JobError
from .version import SERVICE_NAME, VERSION

_MAX_BODY_BYTES_CAP = 1024 * 1024


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
            elif path == "/v1/scan":
                self._send_json(405, _error("method_not_allowed",
                                            "use POST for /v1/scan"),
                                request_id)
                status = 405
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
            if path != "/v1/scan":
                self._send_json(404, _error("not_found",
                                            f"no route for {path}"), request_id)
                status = 404
            else:
                status = self._handle_scan(request_id)
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

        allowed = {"repository", "ref"}
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

        try:
            parsed = github_mod.normalize_repository_url(
                payload["repository"], github_host=self.config.github_host)
            if payload.get("ref"):
                github_mod.validate_ref(payload["ref"])
                parsed["ref"] = payload["ref"]
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
                 wellknown_body):
        self.config = config
        self.service = service
        self.access_logger = access_logger
        self.wellknown_body = wellknown_body
        timeout = getattr(config, "request_timeout_s", 30)
        self.timeout = timeout
        super().__init__(address, handler)
        self.RequestHandlerClass.timeout = timeout
        self.RequestHandlerClass.config = config
        self.RequestHandlerClass.service = service
        self.RequestHandlerClass.access_logger = access_logger
        self.RequestHandlerClass.wellknown_body = wellknown_body

    def handle_error(self, request, client_address):
        # quiet, structured: noisy tracebacks from clients that hang up
        # mid-request are normal in production.
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(config, service: ScanService, access_logger=None,
                wellknown_body=None) -> _Server:
    """Create (not start) the HTTP server bound to config.host:config.port."""
    if access_logger is None:
        from .jsonl import make_access_logger
        access_logger = make_access_logger(config)
    return _Server((config.host, config.port), DocrotHandler, config, service,
                   access_logger, wellknown_body)
