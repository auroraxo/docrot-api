"""SSRF + receipt + logging focused unit tests (T17/T22 complements)."""

import io
import json
import unittest

from docrot_scan_api.jsonl import JsonlLogger
from docrot_scan_api.requestid import new_request_id
from tests.helpers import jsonl_lines


class RequestIdTests(unittest.TestCase):
    def test_shape_and_uniqueness(self):
        ids = {new_request_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)
        for rid in ids:
            self.assertEqual(len(rid), 26)
            self.assertTrue(all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
                                for c in rid))
        self.assertTrue(ids.pop() > "01J")  # time-ordered prefix


class JsonlLoggerTests(unittest.TestCase):
    def test_lines_are_valid_json_with_ts(self):
        stream = io.StringIO()
        log = JsonlLogger("test", stream=stream)
        log.log("hello", a=1, b="two")
        log.access(method="POST", path="/v1/scan", status=200)
        lines = jsonl_lines(stream.getvalue())
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["event"], "hello")
        self.assertEqual(lines[0]["a"], 1)
        self.assertIn("ts", lines[0])
        self.assertEqual(lines[1]["method"], "POST")

    def test_log_file_written(self):
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            log = JsonlLogger("test", path=path)
            log.log("file-event", k="v")
            with open(path) as fh:
                lines = jsonl_lines(fh.read())
            self.assertEqual(lines[0]["event"], "file-event")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
