"""Scan job orchestration: validate -> fetch -> extract -> scan -> check -> respond."""

import time

from . import github, extract as extract_mod, links as links_mod
from .checker import Deadline, check_urls
from .version import SERVICE_NAME, VERSION


class JobError(Exception):
    """Job-level failure surfaced as a structured, honest error response."""

    def __init__(self, code: str, message: str, http_status: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class ScanService:
    def __init__(self, config, job_logger=None, fetcher=None, checker=None):
        self.config = config
        self.job_logger = job_logger
        self._fetcher = fetcher or self._default_fetcher
        self._checker = checker or check_urls

    # -- fetch indirection (tests monkeypatch this) --------------------------

    def _default_fetcher(self, owner, repo, ref):
        url = extract_mod.archive_url(owner, repo, ref,
                                      self.config.codeload_host)
        raw = extract_mod.fetch_archive(
            url,
            max_bytes=self.config.max_archive_bytes,
            connect_timeout_s=self.config.fetch_connect_timeout_s,
            total_timeout_s=min(self.config.fetch_timeout_s,
                                self.config.max_job_seconds),
        )
        return raw

    # -- main entry ------------------------------------------------------------

    def run_scan(self, parsed_repo, request_id: str) -> dict:
        started = time.monotonic()
        wall_start = time.time()
        owner, repo = parsed_repo["owner"], parsed_repo["repo"]
        ref = parsed_repo["ref"] or "HEAD"
        log = self.job_logger

        def elapsed_ms():
            return int((time.monotonic() - started) * 1000)

        self._log("job_started", requestId=request_id, repository=parsed_repo["url"],
                  ref=ref)

        try:
            raw = self._fetcher(owner, repo, ref)
        except extract_mod.FetchError as exc:
            self._log("job_failed", requestId=request_id, code=exc.code,
                      error=exc.message, durationMs=elapsed_ms())
            raise JobError(exc.code, exc.message, exc.http_status) from exc
        except Exception as exc:  # pragma: no cover - defensive
            self._log("job_failed", requestId=request_id, code="fetch_internal_error",
                      error=str(exc), durationMs=elapsed_ms())
            raise JobError("fetch_internal_error", str(exc), 500) from exc

        try:
            result = extract_mod.extract_files(
                raw,
                max_files=self.config.max_files,
                max_file_bytes=self.config.max_file_bytes,
            )
        except extract_mod.FetchError as exc:
            self._log("job_failed", requestId=request_id, code=exc.code,
                      error=exc.message, durationMs=elapsed_ms())
            raise JobError(exc.code, exc.message, exc.http_status) from exc

        if not result.files:
            self._log("job_failed", requestId=request_id, code="no_documentation_files",
                      error="no Markdown/MDX/RST/HTML files found in archive",
                      durationMs=elapsed_ms())
            raise JobError("no_documentation_files",
                           "no Markdown/MDX/RST/HTML files found in the repository "
                           "archive (or all exceeded the per-file size limit)", 422)

        # -- link extraction ---------------------------------------------------

        occurrences = []  # (url, source, line)
        seen_urls = set()
        for relpath in sorted(result.files):
            text = result.files[relpath]
            for url, line in links_mod.extract_links(relpath, text):
                occurrences.append((url, relpath, line))
                seen_urls.add(url)
            if deadline_remaining(self.config.max_job_seconds, started) <= 0:
                self._log("job_failed", requestId=request_id,
                          code="duration_exceeded", error="job deadline exceeded",
                          durationMs=elapsed_ms())
                raise JobError("duration_exceeded",
                               f"scan exceeded {self.config.max_job_seconds}s limit",
                               504)

        if len(seen_urls) > self.config.max_urls:
            msg = (f"scan found {len(seen_urls)} distinct remote URLs; "
                   f"limit is {self.config.max_urls}")
            self._log("job_failed", requestId=request_id, code="too_many_urls",
                      error=msg, durationMs=elapsed_ms())
            raise JobError("too_many_urls", msg, 413)

        self._log("links_extracted", requestId=request_id,
                  scannedFiles=len(result.files),
                  distinctUrls=len(seen_urls),
                  occurrences=len(occurrences))

        # -- URL checking --------------------------------------------------------

        deadline = Deadline(max(0.1, self.config.max_job_seconds -
                                (time.monotonic() - started)))
        outcomes = self._checker(
            sorted(seen_urls),
            deadline=deadline,
            concurrency=self.config.check_concurrency,
            timeout=self.config.check_timeout_s,
            max_redirects=self.config.check_max_redirects,
            max_read_bytes=self.config.check_max_read_bytes,
            user_agent=self.config.check_user_agent,
        )

        broken = []
        for url, source, line in occurrences:
            outcome = outcomes.get(url)
            if outcome is None:
                continue
            if outcome.ok:
                continue
            status = outcome.status
            error = outcome.error
            if status is None and error and error.startswith("ssrf_blocked"):
                status = 403
            broken.append({
                "url": url,
                "status": status,
                "source": source,
                "line": line,
                "error": error,
            })

        duration = elapsed_ms()
        response = {
            "repository": parsed_repo["url"],
            "ref": parsed_repo["ref"],
            "scannedFiles": len(result.files),
            "checkedUrls": len(seen_urls),
            "broken": broken,
            "durationMs": duration,
            "requestId": request_id,
            "receipt": self._build_receipt(request_id, parsed_repo,
                                           len(result.files), len(seen_urls),
                                           broken, duration, wall_start),
        }
        self._log("job_completed", requestId=request_id,
                  repository=parsed_repo["url"], ref=ref,
                  scannedFiles=len(result.files),
                  checkedUrls=len(seen_urls),
                  brokenCount=len(broken),
                  durationMs=duration)
        return response

    # -- helpers -------------------------------------------------------------

    def _build_receipt(self, request_id, parsed_repo, scanned, checked, broken,
                       duration, wall_start):
        cfg = self.config
        return {
            "kind": "scan-completed",
            "requestId": request_id,
            "repository": parsed_repo["url"],
            "ref": parsed_repo["ref"],
            "scannedFiles": scanned,
            "checkedUrls": checked,
            "brokenCount": len(broken),
            "durationMs": duration,
            "completedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(wall_start + duration / 1000)),
            "billing": {
                "model": cfg.billing_model,
                "currency": "SOL",
                "amountDueUsd": cfg.price_usd_per_scan,
                "amountDue": cfg.sol_reference_quote,
                "payTo": cfg.sol_pay_to,
                "terms": ("Payable after result delivery. First external pilot "
                          "scan is free. No automatic billing in this version; "
                          "an operator issues the invoice manually."),
                "pilotFree": False,
            },
        }

    def _log(self, event, **fields):
        if self.job_logger:
            self.job_logger.log(event, service=SERVICE_NAME, version=VERSION,
                                **fields)


def deadline_remaining(total_seconds: float, started_monotonic: float) -> float:
    return total_seconds - (time.monotonic() - started_monotonic)
