import hashlib
import json
import unittest
from pathlib import Path


DEPLOYMENT = Path(__file__).resolve().parents[2] / "scripts" / "deployment"
SHARED_FILES = {"check_ecr_image.py", "summarize_timing.py"}


class SharedDeployToolsTests(unittest.TestCase):
    def test_shared_scripts_match_the_pinned_infra_snapshot(self):
        checksums = json.loads((DEPLOYMENT / "shared-tools.lock.json").read_text())
        self.assertEqual(set(checksums), SHARED_FILES)
        for filename in sorted(SHARED_FILES):
            with self.subTest(filename=filename):
                actual = hashlib.sha256((DEPLOYMENT / filename).read_bytes()).hexdigest()
                self.assertEqual(actual, checksums[filename], "compare with the Infra source before updating")
