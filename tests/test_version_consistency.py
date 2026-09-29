"""Version strings must agree across runtime, metadata, and descriptor.

v1.5.0 deploy lesson: pyproject.toml was bumped but the service still
reported 1.4.1, because the runtime reports docrot_scan_api/version.py.
This test fails if any version source drifts.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _version_py():
    src = (ROOT / "docrot_scan_api" / "version.py").read_text()
    return re.search(r'VERSION = "([^"]+)"', src).group(1)


def _pyproject():
    src = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^version = "([^"]+)"', src, re.M).group(1)


def _descriptor():
    data = json.loads((ROOT / "public" / ".well-known" / "agent-service.json").read_text())
    return data["version"]


def _changelog_top():
    src = (ROOT / "CHANGELOG.md").read_text()
    return re.search(r"^## \[(\d+\.\d+\.\d+)\]", src, re.M).group(1)


class VersionConsistencyTests(unittest.TestCase):
    def test_runtime_metadata_descriptor_agree(self):
        v = _version_py()
        self.assertEqual(_pyproject(), v, "pyproject.toml drifted from version.py")
        self.assertEqual(_descriptor(), v, "agent-service.json drifted from version.py")

    def test_changelog_top_matches_version(self):
        self.assertEqual(_changelog_top(), _version_py(),
                         "CHANGELOG top entry drifted from version.py")


if __name__ == "__main__":
    unittest.main()
