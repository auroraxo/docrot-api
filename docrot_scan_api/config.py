"""Environment-driven configuration with explicit defaults and clamping.

Every limit is documented in docs/API.md. Operators override via env vars;
values are clamped to sane minimums so a bad env var cannot disable a
safety bound entirely.
"""

import os


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


class Config:
    """Immutable-ish runtime configuration snapshot."""

    def __init__(self, env=None):
        if env is not None:  # for tests: inject before reading
            for key, value in env.items():
                os.environ[key] = value
        self.host = os.environ.get("DOCROT_HOST", "127.0.0.1")
        self.port = _int_env("DOCROT_PORT", 8087, 1, 65535)

        # Request limits
        self.max_request_bytes = _int_env(
            "DOCROT_MAX_REQUEST_BYTES", 64 * 1024, 1024, 1024 * 1024
        )
        self.max_header_bytes = _int_env(
            "DOCROT_MAX_HEADER_BYTES", 16 * 1024, 1024, 1024 * 1024
        )
        self.request_timeout_s = _int_env("DOCROT_REQUEST_TIMEOUT_S", 30, 1, 300)

        # Job limits
        self.max_job_seconds = _int_env("DOCROT_MAX_JOB_SECONDS", 240, 5, 1800)
        self.max_archive_bytes = _int_env(
            "DOCROT_MAX_ARCHIVE_BYTES", 50 * 1024 * 1024, 1024, 1024 * 1024 * 1024
        )
        self.max_file_bytes = _int_env(
            "DOCROT_MAX_FILE_BYTES", 2 * 1024 * 1024, 1024, 64 * 1024 * 1024
        )
        self.max_files = _int_env("DOCROT_MAX_FILES", 20000, 10, 200000)
        self.max_urls = _int_env("DOCROT_MAX_URLS", 1500, 1, 20000)

        # URL checking
        self.check_concurrency = _int_env("DOCROT_CHECK_CONCURRENCY", 8, 1, 64)
        self.check_timeout_s = _int_env("DOCROT_CHECK_TIMEOUT_S", 10, 1, 60)
        self.check_max_redirects = _int_env("DOCROT_CHECK_MAX_REDIRECTS", 5, 0, 20)
        self.check_max_read_bytes = _int_env(
            "DOCROT_CHECK_MAX_READ_BYTES", 64 * 1024, 1024, 10 * 1024 * 1024
        )
        self.check_user_agent = os.environ.get(
            "DOCROT_CHECK_USER_AGENT",
            "DocrotScanBot/1.0 (+https://github.com/auroraxo/docrot-api)",
        )

        # GitHub fetch
        self.fetch_timeout_s = _int_env("DOCROT_FETCH_TIMEOUT_S", 60, 1, 600)
        self.fetch_connect_timeout_s = _int_env(
            "DOCROT_FETCH_CONNECT_TIMEOUT_S", 10, 1, 60
        )
        self.github_host = os.environ.get("DOCROT_GITHUB_HOST", "github.com")
        self.codeload_host = os.environ.get("DOCROT_CODELOAD_HOST", "codeload.github.com")

        # Logging
        self.access_log_path = os.environ.get("DOCROT_ACCESS_LOG", "") or None
        self.job_log_path = os.environ.get("DOCROT_JOB_LOG", "") or None
        self.log_level = os.environ.get("DOCROT_LOG_LEVEL", "info").lower()

        # Billing (fixed, not env-configurable on purpose)
        self.price_usd_per_scan = 1.0
        self.sol_pay_to = "CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn"
        self.sol_reference_quote = "0.0065"
        self.billing_model = "manual-invoicing-pilot"

        # Self-serve checkout (direct purchase path)
        job_log = os.environ.get("DOCROT_JOB_LOG", "") or None
        default_store = (os.path.join(os.path.dirname(job_log),
                                      "checkout-orders.json")
                         if job_log else "checkout-orders.json")
        self.checkout_store_path = (
            os.environ.get("DOCROT_CHECKOUT_STORE", "") or default_store)
        self.checkout_ttl_s = _int_env("DOCROT_CHECKOUT_TTL_S", 86400,
                                       300, 604800)
        self.checkout_verify_cooldown_s = _int_env(
            "DOCROT_CHECKOUT_VERIFY_COOLDOWN_S", 10, 0, 3600)
        # Server-side payment watcher (1.9.0+): verify pending orders even
        # when the buyer never polls the status URL. 0 disables the sweep.
        self.checkout_watch_interval_s = _int_env(
            "DOCROT_CHECKOUT_WATCH_INTERVAL_S", 60, 0, 86400)
        self.solana_rpc_url = os.environ.get("DOCROT_SOLANA_RPC", "") or (
            "https://api.mainnet-beta.solana.com")
        self.solana_rpc_timeout_s = _int_env("DOCROT_SOLANA_RPC_TIMEOUT_S",
                                             5, 1, 30)
