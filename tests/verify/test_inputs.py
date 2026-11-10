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
            policy, _ = local_run.read_base_inputs(make_repo.SRC)
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


POLICY = (make_repo.SRC / ".agent-kit" / "policy.yml").read_text()


def base_tree(tmp, policy_text, files=()):
    root = Path(tmp) / "base"
    for rel, text in [(".agent-kit/policy.yml", policy_text), *files]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


class PolicyPaths(unittest.TestCase):
    """Policy paths go through the gate's normalizer; nothing outside the base tree is read."""

    def test_escaping_seed_or_spec_fails_closed(self):
        cases = {
            "seed ..": POLICY.replace("seed: .agent-kit/seed/seed.sql", "seed: ../x"),
            "seed absolute": POLICY.replace("seed: .agent-kit/seed/seed.sql", "seed: /etc/x"),
            "spec ..": POLICY.replace("spec: .agent-kit/journeys/save-profile.spec.ts", "spec: ../x"),
            "spec absolute": POLICY.replace("spec: .agent-kit/journeys/save-profile.spec.ts", "spec: /etc/x"),
            "regress glob ..": POLICY + 'verify: {regress_glob: "../*.spec.ts"}\n',
        }
        for name, text in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                self.assertNotEqual(text, POLICY)
                with self.assertRaisesRegex(SystemExit, "must stay inside the repo"):
                    local_run.read_base_inputs(base_tree(tmp, text))

    def test_dot_slash_seed_reads_the_normalized_file(self):
        text = POLICY.replace("seed: .agent-kit/seed/seed.sql", "seed: ./docs/seed.md") + \
            'verify: {regress_glob: ".//tests/regress/*.spec.ts"}\n'
        with tempfile.TemporaryDirectory() as tmp:
            policy, seed = local_run.read_base_inputs(base_tree(tmp, text, [("docs/seed.md", "select 1;")]))
        self.assertEqual(policy["app"]["db"]["seed"], "docs/seed.md")
        self.assertEqual(seed, b"select 1;")
        self.assertEqual(local_run.regress_glob(policy), "tests/regress/*.spec.ts")

    def test_symlink_out_of_the_base_tree_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = base_tree(tmp, POLICY)
            (Path(tmp) / "secret").write_text("runner file")
            (root / ".agent-kit/seed").mkdir(parents=True)
            (root / ".agent-kit/seed/seed.sql").symlink_to(Path(tmp) / "secret")
            with self.assertRaisesRegex(SystemExit, "outside the base tree"):
                local_run.read_base_inputs(root)


class CallerTemplate(unittest.TestCase):
    def test_kit_ref_placeholder_is_the_init_contract(self):
        text = (ROOT / "templates/repo/.github/workflows/agent-verify.yml").read_text()
        self.assertIn("uses: yudduy/agent-kit/.github/workflows/verify.yml@{{KIT_REF}}\n", text)


if __name__ == "__main__":
    unittest.main()
