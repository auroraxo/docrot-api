"""Docrot Scan API - stdlib-only paid link-rot scanning service for GitHub repos.

Modules:
    config      -- environment-driven limits and constants
    jsonl       -- structured JSONL logging
    requestid   -- ULID-ish request id generation
    github      -- repository URL validation and archive fetching
    extract     -- safe in-memory tar extraction
    links       -- link/image extraction from MD/MDX/RST/HTML
    ssrf        -- SSRF guard (private/reserved/link-local rejection)
    checker     -- bounded-concurrency URL liveness checker
    service     -- scan job orchestration
    server      -- HTTP server and routing
"""

from .config import Config
from .version import VERSION, SERVICE_NAME

__all__ = ["Config", "VERSION", "SERVICE_NAME"]
