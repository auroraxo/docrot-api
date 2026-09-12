"""Structured JSONL logging: one JSON object per line.

Access log: one line per HTTP request.
Job log: one line per scan job lifecycle event.
Streams go to stderr and, optionally, to files (env-configured).
"""

import json
import sys
import threading
import time

_lock = threading.Lock()


def _emit(stream, path, record):
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    line = json.dumps(record, separators=(",", ":"), default=str)
    with _lock:
        stream.write(line + "\n")
        stream.flush()
        if path:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


class JsonlLogger:
    def __init__(self, name: str, path=None, stream=None):
        self.name = name
        self.path = path
        self.stream = stream if stream is not None else sys.stderr

    def log(self, event: str, **fields):
        record = {"logger": self.name, "event": event}
        record.update(fields)
        _emit(self.stream, self.path, record)

    def access(self, **fields):
        self.log("access", **fields)


def make_access_logger(config):
    return JsonlLogger("access", path=config.access_log_path)


def make_job_logger(config):
    return JsonlLogger("job", path=config.job_log_path)
