import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_repo  # noqa: E402
from verify import local_run  # noqa: E402


class BaseInputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.repo = cls.tmp / "repo"
        cls.shas = make_repo.make(cls.repo)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_policy_parses_with_the_kit_yaml_parser_not_yq(self):
        with mock.patch.dict(os.environ, {"PATH": ""}):
            policy = local_run.load_policy(make_repo.SRC / ".agent-kit" / "policy.yml")
        self.assertEqual(policy["app"]["start"]["cmd"], "python3 app.py")
        self.assertEqual(policy["app"]["db"], {"engine": "sqlite", "seed": ".agent-kit/seed/seed.sql"})

    def test_regress_glob_defaults_and_is_configurable(self):
        self.assertEqual(local_run.regress_glob({}), "tests/regress/*.spec.ts")
        self.assertEqual(local_run.regress_glob({"verify": {"regress_glob": "e2e/**/*.spec.ts"}}), "e2e/**/*.spec.ts")

    def test_new_regression_specs_follow_the_glob(self):
        bug, fix = self.shas["bug"], self.shas["fix"]
        self.assertEqual(local_run.new_regression_specs(self.repo, bug, fix, "tests/regress/*.spec.ts"),
                         ["tests/regress/fp-3f9a1c0d7e2b4a65.spec.ts"])
        self.assertEqual(local_run.new_regression_specs(self.repo, bug, fix, "tests/**/*.spec.ts"),
                         ["tests/regress/fp-3f9a1c0d7e2b4a65.spec.ts"])
        self.assertEqual(local_run.new_regression_specs(self.repo, bug, fix, "e2e/*.spec.ts"), [])
        self.assertEqual(local_run.new_regression_specs(self.repo, fix, self.shas["mutant"],
                                                        "tests/regress/*.spec.ts"), [])

    def test_journeys_sha_is_null_without_a_journeys_dir(self):
        self.assertRegex(local_run.journeys_sha(self.repo, self.shas["bug"]), r"^[0-9a-f]{40}$")
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "checkout", "-q", "-b", "no-journeys", self.shas["fix"]], check=True)
        subprocess.run([*git, "rm", "-q", "-r", ".agent-kit/journeys"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "drop journeys"], check=True)
        self.assertIsNone(local_run.journeys_sha(self.repo, "HEAD"))


class CallerTemplate(unittest.TestCase):
    def test_kit_ref_placeholder_is_the_init_contract(self):
        text = (ROOT / "templates/repo/.github/workflows/agent-verify.yml").read_text()
        self.assertIn("uses: yudduy/agent-kit/.github/workflows/verify.yml@{{KIT_REF}}\n", text)


if __name__ == "__main__":
    unittest.main()
