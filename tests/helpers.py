"""Shared test fixtures: config factory, sample archives, HTTP fakes."""

import io
import json
import tarfile
import unittest
from unittest import mock

from docrot_scan_api.config import Config


def make_config(**overrides) -> Config:
    """Config with test-friendly (fast, small) limits, env untouched."""
    defaults = dict(
        DOCROT_MAX_REQUEST_BYTES="65536",
        DOCROT_MAX_JOB_SECONDS="30",
        DOCROT_MAX_ARCHIVE_BYTES=str(1024 * 1024),
        DOCROT_MAX_FILE_BYTES=str(64 * 1024),
        DOCROT_MAX_FILES="500",
        DOCROT_MAX_URLS="100",
        DOCROT_CHECK_CONCURRENCY="4",
        DOCROT_CHECK_TIMEOUT_S="2",
        DOCROT_FETCH_TIMEOUT_S="5",
    )
    saved = {}
    env = {**defaults, **overrides}
    for key, value in env.items():
        saved[key] = None
        import os
        os.environ[key] = value
    cfg = Config()
    import os
    for key in saved:
        os.environ.pop(key, None)
    return cfg


def tar_bytes(members) -> bytes:
    """members: list of (name, kind, payload) where kind in {f, l, d}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, kind, payload in members:
            if kind == "f":
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                ti.mtime = 0
                tf.addfile(ti, io.BytesIO(data))
            elif kind == "l":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = payload
                tf.addfile(ti)
            elif kind == "h":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.LNKTYPE
                ti.linkname = payload
                tf.addfile(ti)
            elif kind == "d":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
    return buf.getvalue()


def simple_repo_tar(extra_members=None) -> bytes:
    members = [
        ("repo/README.md", "f",
         "# Title\n[broken](https://bad.test/gone)\n[ok](https://good.test/page)\n"),
        ("repo/docs/guide.rst", "f",
         "See `the docs <https://good.test/rst>`_ and\n"
         "`dead <https://bad.test/dead-page>`_.\n"),
        ("repo/src/code.py", "f", "print('not scanned')\n"),
    ]
    if extra_members:
        members.extend(extra_members)
    return tar_bytes(members)


class FakeResponse:
    """Minimal context manager faking http.client/urllib responses."""

    def __init__(self, status=200, headers=None, body=b"", reason=""):
        self.status = status
        self.reason = reason
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self.closed = False

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)

    @property
    def headers(self):
        return self._headers

    def read(self, amt=-1):
        if amt is None or amt < 0:
            data, self._body = self._body, b""
        else:
            data, self._body = self._body[:amt], self._body[amt:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self.closed = True


def jsonl_lines(raw: str):
    return [json.loads(line) for line in raw.strip().splitlines() if line.strip()]
