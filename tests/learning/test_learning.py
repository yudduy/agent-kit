import json
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from learning import harvest, judge, pathcheck, proof

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = {"control_repo": "yudduy/agent-control", "repos": ["yudduy/shop"], "agent_logins": []}
PSTACK = ("https://github.com/backnotprop/pstack", "f" * 40)


def control(repos=None):
    root = Path(tempfile.mkdtemp())
    (root / "agent-kit").mkdir()
    (root / "agent-kit" / "repos.json").write_text(json.dumps(repos or {"v": 1, "repos": ["yudduy/shop"]}))
    return root
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

    def test_registry_must_be_the_gate_format(self):
        self.assertEqual(harvest.load_repos(control()), ["yudduy/shop"])
        with self.assertRaises(ValueError):
            harvest.load_repos(control({"v": 1, "repos": {"yudduy/shop": {}}}))

    def test_main_appends_only_new_cases(self):
        path = Path(tempfile.mkdtemp()) / "learning" / "cases.jsonl"
        args = ["--control", str(control()), "--control-repo", "yudduy/agent-control", "--cases", str(path)]
        with mock.patch.object(harvest, "gh_graphql", gql), mock.patch("builtins.print"):
            harvest.main(args)
            harvest.main(args)
        self.assertEqual(len(path.read_text().splitlines()), 6)

    def test_proposal_prompt_carries_cases_and_rules(self):
        client = mock.Mock()
        client.create.return_value = {"id": "bc-1"}
        cases = run()
        harvest.propose(client, "yudduy/shop", "data-not-persisted", cases, PSTACK, "composer-2")
        args, kwargs = client.create.call_args
        prompt = args[2]
        self.assertNotIn("{{", prompt)
        self.assertIn("learn/data-not-persisted", prompt)
        self.assertIn("model:composer-2", prompt)
        self.assertIn("f" * 40, prompt)
        self.assertEqual((args[4], kwargs["model"]), (None, "composer-2"))
        self.assertIn("INV-12", prompt)
        self.assertIn("fails; run it with your change and show it passes", prompt)
        self.assertEqual(prompt.count('"proposed_class": "data-not-persisted"'), 2)
        first = client.create.call_args[0][0]
        harvest.propose(client, "yudduy/shop", "data-not-persisted", cases, PSTACK, "composer-2")
        self.assertEqual(client.create.call_args[0][0], first, "same cases, same attempt id, so Cursor answers 409")


def files(*entries):
    return [{"filename": f, "status": st, "patch": p} for f, st, p in entries]


class PathCheck(unittest.TestCase):
    def test_adding_cases_passes_without_a_label(self):
        found = pathcheck.check(files(("tests/regress/fp-3f9a.spec.ts", "added", "+test('x')"),
                                      ("evals/cases.jsonl", "modified", "@@ -3,0 +4 @@\n+{\"id\": 4}")))
        self.assertEqual(found, ([], []))

    def test_editing_existing_expectations_fails(self):
        cases = [("evals/evals.json", "modified", '@@ -4 +4 @@\n-  "expected": "pass"\n+  "expected": "fail"'),
                 ("evals/evals.json", "modified", '@@ -4 +4,2 @@\n-  "expected": "pass"\n+  "expected": "pass",\n+  "expected": "fail"'),
                 ("tests/test_save.py", "modified", "@@ -10,0 +11 @@\n+    return  # switches the test off")]
        for case in cases:
            problems, _ = pathcheck.check(files(case))
            self.assertEqual(len(problems), 1, case)

    def test_policy_graders_thresholds_gate_and_verify_fail(self):
        problems, _ = pathcheck.check(files((".agent-kit/policy.yml", "modified", "-a\n+b"), ("evals/grade.py", "added", "+x"),
                                            ("gate/decide.py", "modified", "+x"), ("verify/observe.py", "modified", "+x"),
                                            (".agent-kit/journeys/save.spec.ts", "modified", "+x"),
                                            (".github/workflows/agent-verify.yml", "modified", "+x"),
                                            ("config/thresholds.json", "modified", "+x"), ("tests/old_test.py", "removed", "-x")))
        self.assertEqual(len(problems), 8)

    def test_higher_level_fixes_pass_as_governance(self):
        problems, governance = pathcheck.check(files((".cursor/skills/verify-shop/SKILL.md", "modified", "-a\n+b"),
                                                     ("scripts/lint_profile_save.py", "added", "+x"),
                                                     (".github/workflows/profile-lint.yml", "added", "+x"),
                                                     ("tests/test_lint.py", "added", "+x")))
        self.assertEqual(problems, [])
        self.assertEqual(governance, [".cursor/skills/verify-shop/SKILL.md", "scripts/lint_profile_save.py", ".github/workflows/profile-lint.yml"])

    def test_added_test_hooks_are_not_cases(self):
        for hook in ("tests/conftest.py", "fixtures/conftest.py", "tests/__init__.py", "playwright.config.ts", "tests/setup.ts"):
            problems, governance = pathcheck.check(files((hook, "added", "+x")))
            self.assertEqual((problems, governance), ([], [hook]), hook)

    def test_editing_an_existing_test_or_hook_fails(self):
        for path in ("tests/test_setup.py", "playwright.config.ts", "tests/conftest.py"):
            problems, _ = pathcheck.check(files((path, "modified", "-    assert authorized\n+    pass")))
            self.assertEqual(len(problems), 1, path)

    def test_exit_codes(self):
        path = Path(tempfile.mkdtemp()) / "files.jsonl"
        for entries, code in [((("tests/a.spec.ts", "added", "+x"),), 0), ((("src/check.py", "added", "+x"),), 10),
                              ((("verify/observe.py", "modified", "+x"),), 1)]:
            path.write_text("\n".join(json.dumps(f) for f in files(*entries)))
            with mock.patch("builtins.print"):
                self.assertEqual(pathcheck.main(["--files", str(path)]), code)


class FakeGitHubAPI:
    def __init__(self, head="b" * 40, body="<!-- agent-kit:learning v1 class:data-not-persisted model:composer-2 -->"):
        self.head, self.posted = head, []
        self.prs = [{"number": 7, "head": {"ref": "learn/data-not-persisted", "sha": head}, "base": {"sha": "a" * 40}, "body": body},
                    {"number": 8, "head": {"ref": "cursor/fix-x", "sha": "c" * 40}, "base": {"sha": "a" * 40}, "body": ""}]
        self.comments = []

    def __call__(self, method, path, body=None):
        import base64
        if method == "POST":
            self.posted.append((path, body))
            return {}
        if path == "/user":
            return {"login": "agent-kit-judge"}
        if "/contents/" in path:
            text = "OLD: check the toast." if path.endswith("a" * 40) else "NEW: query the profiles row."
            return {"content": base64.b64encode(text.encode()).decode()}
        raise AssertionError(path)

    def pages(self, path):
        if path.endswith("/pulls?state=open"):
            return self.prs
        if path.endswith("/pulls/7/files"):
            return [{"filename": "src/f%d.py" % i, "status": "added"} for i in range(150)] + [
                {"filename": ".cursor/skills/verify-shop/SKILL.md", "status": "modified"}]
        if path.endswith("/issues/7/comments"):
            return self.comments
        raise AssertionError(path)


class Judge(unittest.TestCase):
    def run_judge(self, api, answer):
        prompts = []
        ask = lambda prompt: prompts.append(prompt) or answer(prompt)
        judge.run(["yudduy/shop"], api, api.pages, ask, "gpt-judge")
        return prompts

    def test_verdict_maps_back_whichever_order_was_shown(self):
        orders = set()
        for head in ("b" * 40, "d" * 40, "e" * 40, "1" * 40):
            api = FakeGitHubAPI(head=head)
            def prefer_new(prompt):
                orders.add(prompt.index("NEW:") < prompt.index("OLD:"))
                return '{"better": "%s", "reason": "x"}' % ("A" if prompt.index("NEW:") < prompt.index("OLD:") else "B")
            self.run_judge(api, prefer_new)
            self.assertIn('"verdict":"new"', api.posted[0][1]["body"])
        self.assertEqual(orders, {True, False}, "both orders are exercised")

    def test_blind_verdict_is_mapped_back_and_posted_once(self):
        api = FakeGitHubAPI()
        def prefer_new(prompt):
            label = "A" if prompt.index("NEW:") < prompt.index("OLD:") else "B"
            return '{"better": "%s", "reason": "It checks stored data."}' % label
        prompts = self.run_judge(api, prefer_new)
        self.assertEqual(len(prompts), 1)
        for leak in ("old version", "new version", "proposed", "current text"):
            self.assertNotIn(leak, prompts[0].lower())
        self.assertIn("Version A", prompts[0])
        self.assertEqual(len(api.posted), 1, "the cursor/ PR is not a learning PR")
        path, body = api.posted[0]
        self.assertTrue(path.endswith("/issues/7/comments"))
        self.assertIn('"verdict":"new"', body["body"])
        self.assertIn("b" * 40, body["body"])
        api.comments = [{"body": body["body"], "user": {"login": "mallory"}}]
        self.assertEqual(len(self.run_judge(api, prefer_new)), 1, "a copied marker from another login does not count")
        api.comments = [{"body": body["body"], "user": {"login": "agent-kit-judge"}}]
        self.assertEqual(self.run_judge(api, prefer_new), [])

    def test_malformed_answer_is_recorded_as_unclear(self):
        api = FakeGitHubAPI()
        self.run_judge(api, lambda p: '{"better": "A", "reason": }')
        self.assertIn('"verdict":"unclear"', api.posted[0][1]["body"])

    def test_unknown_proposer_is_not_judged(self):
        api = FakeGitHubAPI(body="")
        self.assertEqual(self.run_judge(api, lambda p: "{}"), [])
        self.assertIn("proposer", api.posted[0][1]["body"])

    def test_proposer_from_the_judge_family_is_not_judged(self):
        api = FakeGitHubAPI(body="<!-- agent-kit:learning v1 class:x model:gpt-5 -->")
        self.assertEqual(self.run_judge(api, lambda p: "{}"), [])
        self.assertIn("another model family", api.posted[0][1]["body"])


    def test_a_broken_stored_marker_is_ignored(self):
        api = FakeGitHubAPI()
        api.comments = [{"body": "<!-- agent-kit:judge v1 {broken} -->", "user": {"login": "agent-kit-judge"}}]
        self.assertEqual(len(self.run_judge(api, lambda p: '{"better": "tie", "reason": "x"}')), 1)


OPEN_PR = {"number": 21, "title": "Fix cart total", "url": "https://github.com/yudduy/shop/pull/21", "body": "", "mergedAt": None,
           "headRefName": "cursor/fix-cart-e5f6", "author": {"login": "cursor", "__typename": "Bot"},
           "userContentEdits": {"nodes": []}, "comments": {"nodes": []}, "reviews": {"nodes": []},
           "commits": {"nodes": [{"commit": {"oid": "3" * 40, "checkSuites": {"nodes": [{"checkRuns": {"nodes": [
               {"conclusion": "FAILURE", "summary": "```json\n{\"v\": 1, \"reasons\": [\"evidence missing\"]}\n```",
                "url": "https://github.com/yudduy/shop/runs/3", "completedAt": "2026-11-08T08:00:00Z"}]}}]}}}]}}


class HarvestUnmerged(unittest.TestCase):
    def test_gate_failures_on_open_or_closed_agent_prs_become_cases(self):
        def search_like_github(query, q, n, after):
            nodes = json.loads((FIXTURES / ("incidents.json" if "is:issue" in q else "prs.json")).read_text())
            if "is:pr" in q and "is:merged" not in q:
                nodes.append(OPEN_PR)
            return {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
        cases = harvest.Harvest(CONFIG, search_like_github, lambda: datetime(2026, 11, 10, 9, tzinfo=timezone.utc)).run(21)
        unmerged = [c for c in cases if c["pr"] == 21]
        self.assertEqual([(c["signal"], c["proposed_class"]) for c in unmerged], [("gate_failure", "gate-evidence-missing")])
        self.assertEqual(Counter(c["signal"] for c in cases if c["pr"] != 21),
                         {"review_comment": 2, "duy_edit": 1, "gate_failure": 1, "revert_14d": 1, "false_green": 1})


def tree(**files):
    root = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


CASE = 'import app\nassert app.save("Ada") == "Ada"\n'
RUN_CASE = '"%s" {case}' % sys.executable
PASS, FAIL = '"%s" -c "pass"' % sys.executable, '"%s" -c "raise SystemExit(1)"' % sys.executable


class Proof(unittest.TestCase):
    def prove(self, case=CASE, base_save="return None", suite=PASS, case_cmd=RUN_CASE):
        base = tree(**{"app.py": "def save(name):\n    %s\n" % base_save})
        head = tree(**{"app.py": "def save(name):\n    return name\n", "test_save_case.py": case})
        entry = ("test_save_case.py", "added", "\n".join("+" + l for l in case.splitlines()))
        with mock.patch("builtins.print"):
            return proof.prove(files(entry), base, head, case_cmd, suite)

    def test_an_added_case_with_no_assertion_is_not_proof(self):
        self.assertEqual(pathcheck.check(files(("test_save_case.py", "added", ""))), ([], []), "the path check alone passes it")
        for case in ("", "import app\n", "import app\n# assert app.save('Ada')\n"):
            ok, counts = self.prove(case=case)
            self.assertFalse(ok, repr(case))
            self.assertEqual(counts["no_assertion"], 1, repr(case))

    def test_assertion_words_inside_strings_do_not_count_and_go_checks_do(self):
        self.assertFalse(proof.asserts(['print("expected")', "log('should work')"]))
        self.assertFalse(proof.asserts(['message = """', "expected", '"""']))
        self.assertFalse(proof.asserts(["const m = `", "should", "`;"]))
        self.assertTrue(proof.asserts(['doc = """x"""', "assert save()"]))
        self.assertTrue(proof.asserts(['x = "`"', "assert True", 'y = "`"']))
        self.assertFalse(proof.asserts(["const message = `hello \\` expect nope`;"]))
        self.assertTrue(proof.asserts(['\tt.Fatalf("bad value")']))
        self.assertTrue(proof.asserts(["    require.Equal(t, 1, got)"]))
        self.assertTrue(proof.asserts(['{"input": "Ada", "expected": "Ada"}'], jsonl=True))
        self.assertFalse(proof.asserts(['{"input": "expected"}'], jsonl=True))

    def test_a_case_that_fails_on_base_and_passes_on_head_with_the_suite_green_is_proof(self):
        ok, counts = self.prove()
        self.assertTrue(ok)
        self.assertEqual(counts, {"cases": 1, "failed_on_base": 1, "passed_on_head": 1, "no_assertion": 0, "suite": "pass"})

    def test_a_case_that_already_passes_on_base_is_not_proof(self):
        ok, counts = self.prove(base_save="return name")
        self.assertFalse(ok)
        self.assertEqual(counts["failed_on_base"], 0)

    def test_a_failing_full_suite_on_head_is_not_proof(self):
        ok, counts = self.prove(suite=FAIL)
        self.assertFalse(ok)
        self.assertEqual(counts["suite"], "fail")

    def test_no_added_case_or_no_test_command_is_not_proof(self):
        base, head = tree(**{"app.py": "x = 1\n"}), tree(**{"app.py": "x = 2\n"})
        with mock.patch("builtins.print"):
            self.assertFalse(proof.prove(files(("app.py", "modified", "-x = 1\n+x = 2")), base, head, RUN_CASE, PASS)[0])
            self.assertFalse(self.prove(case_cmd="")[0])

    def test_lines_appended_to_a_case_list_run_as_one_case(self):
        base = tree(**{"app.py": "def save(name):\n    return None\n", "evals/cases.jsonl": '{"name": "Bo", "expected": null}\n'})
        lines = '{"name": "Bo", "expected": null}\n{"name": "Ada", "expected": "Ada"}\n'
        head = tree(**{"app.py": "def save(name):\n    return name if name == 'Ada' else None\n", "evals/cases.jsonl": lines})
        runner = ('"%s" -c "import json,sys,app; sys.exit(any(app.save(c[\'name\']) != c[\'expected\'] for c in map(json.loads, open(sys.argv[1]))))" {case}'
                  % sys.executable)
        with mock.patch("builtins.print"):
            ok, counts = proof.prove(files(("evals/cases.jsonl", "modified", '@@ -1,0 +2 @@\n+{"name": "Ada", "expected": "Ada"}')),
                                     base, head, runner, PASS)
        self.assertTrue(ok)
        self.assertEqual(counts["cases"], 1)

    def test_main_writes_the_counts_to_the_summary(self):
        base, head = tree(**{"app.py": "def save(name):\n    return None\n"}), tree(**{"app.py": "def save(name):\n    return name\n", "test_save_case.py": CASE})
        listing = Path(tempfile.mkdtemp()) / "files.jsonl"
        listing.write_text(json.dumps({"filename": "test_save_case.py", "status": "added", "patch": "".join("+" + l for l in CASE.splitlines(True))}))
        summary = listing.with_name("summary.md")
        summary.write_text("Learning proof passes: planted by PR code\n")
        with mock.patch("builtins.print"):
            code = proof.main(["--files", str(listing), "--base", str(base), "--head", str(head), "--case-cmd", RUN_CASE,
                               "--suite-cmd", PASS, "--summary", str(summary)])
        self.assertEqual(code, 0)
        self.assertIn("1 of 1 added cases fail on base", summary.read_text())
        self.assertNotIn("planted", summary.read_text())


if __name__ == "__main__":
    unittest.main()
