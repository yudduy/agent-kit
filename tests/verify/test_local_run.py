"""End-to-end: the verify flow against the fixture app with real Playwright.

Opt-in (needs node, npm and network for the first Playwright install):
    AGENT_KIT_E2E=1 python3 -m unittest discover -s tests/verify
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_repo  # noqa: E402
from verify import evidence, local_run  # noqa: E402


@unittest.skipUnless(os.environ.get("AGENT_KIT_E2E") and shutil.which("node"), "set AGENT_KIT_E2E=1")
class LocalRun(unittest.TestCase):
    def run_flow(self, tmp, base, head):
        out = Path(tmp) / f"out-{head}"
        code = local_run.main(["--repo", f"{tmp}/repo", "--base", self.shas[base], "--head", self.shas[head],
                               "--out", str(out), "--work", f"{tmp}/work", "--isolation", "host"])
        doc = json.loads((out / "evidence.json").read_text())
        self.assertEqual(evidence.errors(doc, out), [])
        return code, doc

    def test_fix_passes_and_mutant_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.shas = make_repo.make(Path(tmp) / "repo")
            code, doc = self.run_flow(tmp, "bug", "fix")
            self.assertEqual((code, doc["result"]), (0, "pass"))
            self.assertEqual([(r["fail_on_base"], r["pass_on_head"]) for r in doc["regression"]], [(True, True)])

            code, doc = self.run_flow(tmp, "fix", "mutant")
            [scenario] = doc["scenarios"]
            self.assertEqual((code, doc["result"], scenario["ui_result"]), (1, "fail", "pass"))
            self.assertEqual(scenario["data_assertions"][0]["observed"], [{"name": "Grace"}])


if __name__ == "__main__":
    unittest.main()
