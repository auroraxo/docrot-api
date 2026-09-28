#!/usr/bin/env python3
"""Replay the documented error contract against a live deployment.

Every case in docs/API.md's error table that can be triggered without
upstream abuse is sent through the *public* edge and must come back with
exactly the documented HTTP status, a JSON body, and the documented
`error.code`. This is the check that caught Cloudflare replacing origin
502/504 bodies (see "Why no 502/504?" in docs/API.md) — it must run
after every deploy, because the contract lives where customers connect,
not at the origin.

Usage:
    python3 tools/edge_contract_replay.py                # production edge
    python3 tools/edge_contract_replay.py http://127.0.0.1:8087
    python3 tools/edge_contract_replay.py https://codebyaurora.com/docrot-api

Exit code 0 only if every case passes.
"""
import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "https://codebyaurora.com/docrot-api"

# (name, method, path, headers, body, expected_status, expected_code)
CASES = [
    ("unknown route", "GET", "/unknown-route-replay", {}, None, 404, "not_found"),
    ("GET on scan route", "GET", "/v1/scan", {}, None, 405, "method_not_allowed"),
    ("wrong content type", "POST", "/v1/scan",
     {"Content-Type": "text/plain"}, b"hello", 415, "unsupported_media_type"),
    ("invalid JSON body", "POST", "/v1/scan",
     {"Content-Type": "application/json"}, b"{not-json", 400, "invalid_json"),
    ("JSON not an object", "POST", "/v1/scan",
     {"Content-Type": "application/json"}, b'["array"]', 400, "invalid_json"),
    ("missing repository", "POST", "/v1/scan",
     {"Content-Type": "application/json"}, b"{}", 400, "missing_field"),
    ("unknown field", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     b'{"repository":"https://github.com/a/b","extra":1}', 400, "unknown_field"),
    ("ref not a string", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     b'{"repository":"https://github.com/a/b","ref":123}', 400, "invalid_field"),
    ("non-https scheme", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     b'{"repository":"http://github.com/a/b"}', 422, "unsupported_repository_scheme"),
    ("non-github host", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     b'{"repository":"https://gitlab.com/a/b"}', 422, "unsupported_repository_host"),
    ("query in repository", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     b'{"repository":"https://github.com/a/b?query=1"}', 422, "invalid_repository"),
    ("bad repository shape", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     b'{"repository":"https://github.com/a"}', 422, "invalid_repository"),
    ("unsafe ref characters", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     b'{"repository":"https://github.com/a/b","ref":"bad ref with spaces"}',
     422, "invalid_ref"),
    ("oversized request body", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     json.dumps({"repository": "https://github.com/a/b",
                 "pad": "x" * 70000}).encode(), 413, "request_too_large"),
    # Edge-safe 502/504 replacements — the cases the CDN used to eat.
    ("repo not found (edge-safe 404)", "POST", "/v1/scan",
     {"Content-Type": "application/json"},
     b'{"repository":"https://github.com/auroraxo/this-repo-does-not-exist-9x7"}',
     404, "repository_or_ref_not_found"),
]


# Cloudflare managed-challenges the default Python-urllib UA (403) but passes
# an explicit client UA — verified against production. Always announce yourself.
USER_AGENT = "docrot-contract-replay/1.0 (edge contract verification)"


def request(base, method, path, headers, body, timeout):
    headers = {"User-Agent": USER_AGENT, **headers}
    req = urllib.request.Request(base + path, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def main():
    base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else DEFAULT_BASE
    print(f"edge contract replay against {base}\n")
    failures = []

    # Service identity first: health and agent descriptor must agree.
    for path, name in (("/health", "health"), ("/.well-known/agent-service.json",
                                               "agent-service")):
        status, ctype, raw = request(base, "GET", path, {}, None, 15)
        ok = status == 200 and "application/json" in ctype
        try:
            version = json.loads(raw).get("version", "?")
        except ValueError:
            version = "?"
            ok = False
        print(f"  [{'ok' if ok else 'FAIL'}] {name}: 200 JSON, version {version}")
        if not ok:
            failures.append(name)

    for name, method, path, headers, body, exp_status, exp_code in CASES:
        timeout = 90 if "edge-safe" in name else 15
        try:
            status, ctype, raw = request(base, method, path, headers, body, timeout)
        except (urllib.error.URLError, OSError) as exc:
            print(f"  [FAIL] {name}: transport error {exc}")
            failures.append(name)
            continue
        problems = []
        if status != exp_status:
            problems.append(f"status {status} != {exp_status}")
        if "application/json" not in ctype:
            problems.append(f"content-type {ctype!r} is not JSON (edge ate the body?)")
        else:
            try:
                code = json.loads(raw)["error"]["code"]
                if code != exp_code:
                    problems.append(f"error.code {code!r} != {exp_code!r}")
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(f"body is not the error envelope: {exc}")
        if problems:
            print(f"  [FAIL] {name}: {'; '.join(problems)}")
            failures.append(name)
        else:
            print(f"  [ok]   {name}: {status} {exp_code}")

    total = len(CASES) + 2
    print(f"\n{total - len(failures)}/{total} passed"
          + (f" — FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
