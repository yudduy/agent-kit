import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from learning import harvest, pathcheck

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = {"control_repo": "yudduy/agent-control", "repos": {"yudduy/shop": {"branch": "main"}}, "agent_logins": []}
SCHEMA = json.loads((Path(__file__).parents[2] / "schemas" / "learning-case.v1.json").read_text())


def gql(query, q, n, after):
    nodes = json.loads((FIXTURES / ("incidents.json" if "is:issue" in q else "prs.json")).read_text())
    return {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}


def run():
    return harvest.Harvest(CONFIG, gql, lambda: datetime(2026, 11, 10, 9, tzinfo=timezone.utc)).run(21)


class Harvest(unittest.TestCase):
    def test_signals_become_cases_and_bots_and_lgtm_are_ignored(self):
        cases = run()
        self.assertEqual(Counter(c["signal"] for c in cases),
                         {"review_comment": 2, "duy_edit": 1, "gate_failure": 1, "revert_14d": 1, "false_green": 1})
        excerpts = " ".join(c["excerpt"] for c in cases)
        self.assertNotIn("does not assert the database row", excerpts)  # coderabbitai[bot]
        self.assertNotIn("I added a regression test", excerpts)  # the agent itself
        self.assertNotIn("LGTM", excerpts)
        self.assertEqual({c["pr"] for c in cases}, {12, 15})

    def test_two_cases_make_a_class_qualify(self):
        cases = run()
        self.assertEqual(harvest.newly_qualified([], cases), [("yudduy/shop", "data-not-persisted")])
        self.assertEqual(harvest.newly_qualified(cases, []), [], "a class qualifies once, not every week")

    def test_cases_match_the_c6_schema(self):
        for case in run():
            self.assertEqual(set(case), set(SCHEMA["required"]))
            self.assertRegex(case["id"], SCHEMA["properties"]["id"]["pattern"])
            self.assertIn(case["signal"], SCHEMA["properties"]["signal"]["enum"])
            self.assertLessEqual(len(case["excerpt"]), 280)
            self.assertRegex(case["proposed_class"], SCHEMA["properties"]["proposed_class"]["pattern"])
            self.assertTrue(case["evidence"].startswith("https://github.com/"))

    def test_main_appends_only_new_cases(self):
        path = Path(tempfile.mkdtemp()) / "learning" / "cases.jsonl"
        config = Path(tempfile.mkdtemp()) / "agent-kit.json"
        config.write_text(json.dumps(CONFIG))
        with mock.patch.object(harvest, "gh_graphql", gql), mock.patch("builtins.print"):
            harvest.main(["--config", str(config), "--cases", str(path)])
            harvest.main(["--config", str(config), "--cases", str(path)])
        self.assertEqual(len(path.read_text().splitlines()), 6)

    def test_proposal_prompt_carries_cases_and_rules(self):
        client = mock.Mock()
        client.create.return_value = {"id": "bc-1"}
        cases = run()
        harvest.propose(client, CONFIG, "yudduy/shop", "data-not-persisted", cases)
        prompt = client.create.call_args[0][2]
        self.assertNotIn("{{", prompt)
        self.assertIn("INV-12", prompt)
        self.assertIn("fails; run it with your change and show it passes", prompt)
        self.assertEqual(prompt.count('"proposed_class": "data-not-persisted"'), 2)
        first = client.create.call_args[0][0]
        harvest.propose(client, CONFIG, "yudduy/shop", "data-not-persisted", cases)
        self.assertEqual(client.create.call_args[0][0], first, "same cases, same attempt id, so Cursor answers 409")


def files(*entries):
    return [{"filename": f, "status": s, "patch": p} for f, s, p in entries]


class PathCheck(unittest.TestCase):
    def test_adding_a_case_passes(self):
        self.assertEqual(pathcheck.problems(files(("tests/regress/fp-3f9a.spec.ts", "added", "+test('x')"),
                                                  ("evals/cases.jsonl", "modified", "@@ -3,0 +4 @@\n+{\"id\": 4}"),
                                                  ("evals/evals.json", "modified", "@@ -9,2 +9,3 @@\n-  }\n+  },\n+  {\"id\": 9}"))), [])

    def test_editing_an_existing_expectation_fails(self):
        found = pathcheck.problems(files(("evals/evals.json", "modified", "@@ -4 +4 @@\n-  \"expected\": \"pass\"\n+  \"expected\": \"fail\"")))
        self.assertEqual(len(found), 1)
        self.assertIn("existing case", found[0])

    def test_protected_and_non_case_files_fail(self):
        found = pathcheck.problems(files((".agent-kit/policy.yml", "modified", "-a\n+b"), ("evals/grade.py", "added", "+x"),
                                         ("gate/decide.py", "modified", "+x"), ("src/app.py", "modified", "+x"),
                                         ("tests/old_test.py", "removed", "-x")))
        self.assertEqual([f.split(":")[0].split(" ")[0] for f in found],
                         [".agent-kit/policy.yml", "evals/grade.py", "gate/decide.py", "src/app.py", "tests/old_test.py"])

    def test_cli_reads_gh_jsonl(self):
        path = Path(tempfile.mkdtemp()) / "files.jsonl"
        path.write_text("\n".join(json.dumps(f) for f in files(("verify/observe.py", "modified", "+x"))))
        with mock.patch("builtins.print"):
            self.assertEqual(pathcheck.main(["--files", str(path)]), 1)


if __name__ == "__main__":
    unittest.main()
