import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from verify import evidence  # noqa: E402

SHA = "a" * 40


def good_doc():
    return {
        "schema": 1, "repo": "owner/name", "pr": 12, "head_sha": SHA, "base_sha": "b" * 40, "kit_ref": "c" * 40,
        "policy_sha": "d" * 40, "journeys_sha": "e" * 40,
        "run": {"id": 1, "attempt": 1, "workflow_ref": f"yudduy/agent-kit/.github/workflows/verify.yml@{'c' * 40}"},
        "env": {"runner_image": "ubuntu24/20261101.1", "app_image_digest": "sha256:" + "1" * 64,
                "db_image_digest": None, "seed_hash": "sha256:" + "2" * 64},
        "scenarios": [{"id": "save-profile", "source": "base", "ui_result": "pass",
                       "data_assertions": [{"id": "name-persisted", "sql_sha256": "3" * 64,
                                            "expected": [{"name": "Ada"}], "observed": [{"name": "Ada"}],
                                            "pass": True}],
                       "result": "pass", "trace": "traces/save-profile.zip"}],
        "regression": [{"id": "fp-3f9a", "spec": "tests/regress/fp-3f9a.spec.ts", "trusted": False,
                        "fail_on_base": True, "pass_on_head": True}],
        "artifacts": [{"name": "traces/save-profile.zip", "sha256": "4" * 64}],
        "result": "pass",
    }


def mutate(fn):
    doc = copy.deepcopy(good_doc())
    fn(doc)
    return doc


class SchemaTest(unittest.TestCase):
    def test_good_document_is_valid(self):
        self.assertEqual(evidence.errors(good_doc()), [])

    def test_bad_documents_are_rejected(self):
        cases = {
            "wrong schema version": lambda d: d.update(schema=2),
            "short head sha": lambda d: d.update(head_sha="abc"),
            "extra field": lambda d: d.update(verdict="trust me"),
            "missing env": lambda d: d.pop("env"),
            "trusted regression": lambda d: d["regression"][0].update(trusted=True),
            "mutable workflow ref": lambda d: d["run"].update(
                workflow_ref="yudduy/agent-kit/.github/workflows/verify.yml@v1"),
            "bool as integer": lambda d: d.update(pr=True),
            "unprefixed digest": lambda d: d["env"].update(seed_hash="2" * 64),
            "pass with failing assertion": lambda d: d["scenarios"][0]["data_assertions"][0].update({"pass": False}),
            "pass with no assertions": lambda d: d["scenarios"][0].update(data_assertions=[]),
            "pass with failing ui": lambda d: d["scenarios"][0].update(ui_result="fail"),
            "pass with no scenarios": lambda d: d.update(scenarios=[], artifacts=[]),
            "trace not in artifacts": lambda d: d.update(artifacts=[]),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                self.assertNotEqual(evidence.errors(mutate(fn)), [])

    def test_regression_never_decides_the_result(self):
        doc = mutate(lambda d: d["regression"][0].update(pass_on_head=False))
        self.assertEqual(evidence.errors(doc), [])

    def test_artifact_hashes_are_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "traces").mkdir()
            (Path(tmp) / "traces" / "save-profile.zip").write_bytes(b"trace")
            doc = good_doc()
            self.assertIn("artifact traces/save-profile.zip: sha256 mismatch", evidence.errors(doc, tmp))
            doc["artifacts"][0]["sha256"] = evidence.sha256_bytes(b"trace")
            self.assertEqual(evidence.errors(doc, tmp), [])
            doc["artifacts"][0]["name"] = doc["scenarios"][0]["trace"] = "../outside.zip"
            self.assertIn("artifact ../outside.zip: missing", evidence.errors(doc, tmp))

    def test_cli_checks_run_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text(json.dumps(mutate(lambda d: d.update(artifacts=[], scenarios=[
                {**d["scenarios"][0], "trace": None}]))))
            self.assertEqual(evidence.main([str(path), "--head-sha", SHA, "--pr", "12", "--run-id", "1"]), 0)
            self.assertEqual(evidence.main([str(path), "--head-sha", "f" * 40]), 1)


class RegressionTest(unittest.TestCase):
    def test_fail_on_base_pass_on_head(self):
        spec = "tests/regress/fp-3f9a.spec.ts"
        self.assertEqual(evidence.regression_entry(spec, passed_on_base=False, passed_on_head=True),
                         {"id": "fp-3f9a", "spec": spec, "trusted": False, "fail_on_base": True,
                          "pass_on_head": True})
        entry = evidence.regression_entry(spec, passed_on_base=True, passed_on_head=True)
        self.assertFalse(entry["fail_on_base"])


if __name__ == "__main__":
    unittest.main()
