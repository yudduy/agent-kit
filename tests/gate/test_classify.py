import unittest

from gate.classify import classify
from gate.policy import parse
from tests.gate.helpers import KIT, POLICY, change

POLICY_DICT = parse(POLICY + 'classes:\n  billing-ui: {paths: ["src/billing/**"], floor: payments}\n  search: {added_lines: ["elastic"]}\n')


def cls(*files, policy=POLICY_DICT):
    return classify(list(files), policy)


class ClassifyTest(unittest.TestCase):
    def test_docs_only_is_docs(self):
        self.assertEqual(cls(change("docs/guide.md"), change("README.md"), change("docs/img/a.txt")), ("docs", ["docs"]))

    def test_governance_paths_inv9_inv12(self):
        for path in (".agent-kit/policy.yml", ".agent-kit/journeys/save-profile.spec.ts", ".github/workflows/ci.yml",
                     ".github/README.md", ".cursor/skills/verify-app/SKILL.md", "CODEOWNERS", "docs/CODEOWNERS",
                     "AGENTS.md", "src/AGENTS.md", "CLAUDE.md", "db/seed.sql", "evals/grader.py", "fixtures/profile.md",
                     "tests/fixtures/sentry/event.json", "skills/evals/cases.json",
                     "SKILL.md", "tools/x/SKILL.md", "skills/verify/run.py", "pkg/skills/a.txt", ".claude/settings.json",
                     ".agents/skills/x/SKILL.md", "docs/design/invariants.md", "docs/AGENTS.md"):
            with self.subTest(path):
                self.assertEqual(cls(change(path))[0], "governance")

    def test_feature_docs_stay_docs(self):
        self.assertEqual(cls(change("docs/features/profile.md")), ("docs", ["docs"]))

    def test_learning_branch_is_always_governance_inv11(self):
        self.assertEqual(classify([change("docs/features/profile.md")], POLICY_DICT, head_ref="learn/case-3f9a"), ("governance", ["governance", "docs"]))
        self.assertEqual(classify([change("docs/features/profile.md")], POLICY_DICT, head_ref="agent/learn/x")[0], "docs")

    def test_floor_holds_without_repo_policy(self):
        self.assertEqual(cls(change(".github/workflows/ci.yml"), policy=None)[0], "governance")
        self.assertEqual(cls(change(".agent-kit/policy.yml"), policy={})[0], "governance")
        self.assertEqual(cls(change("fixtures/a.md"), policy=None)[0], "governance")

    def test_renames_count_both_paths(self):
        self.assertEqual(cls(change("ci.yml", status="renamed", previous_filename=".github/workflows/ci.yml"))[0], "governance")
        self.assertEqual(cls(change("docs/save.md", status="renamed", previous_filename="src/profile/save.ts")), ("review", ["review", "docs"]))

    def test_migration_path_is_never_docs_or_auto(self):
        self.assertEqual(cls(change("db/migrations/001.sql"))[0], "migrations")
        self.assertEqual(cls(change("db/migrations/001.sql"), policy=None)[0], "review")

    def test_destructive(self):
        self.assertEqual(cls(change("src/a.py", status="removed", patch=None))[0], "destructive")
        self.assertEqual(cls(change("docs/ops.md", patch="@@ -0,0 +1 @@\n+run DROP TABLE users"))[0], "destructive")
        self.assertEqual(cls(change("scripts/x.sh", patch="@@ -0,0 +1 @@\n+rm -rf /tmp/x"))[0], "destructive")
        self.assertEqual(cls(change("docs/ops.md", patch="@@ -1 +0,0 @@\n-DROP TABLE users"))[0], "docs")

    def test_most_restrictive_wins(self):
        self.assertEqual(cls(change("docs/a.md"), change("src/app.py")), ("review", ["review", "docs"]))
        self.assertEqual(cls(change(".github/x.yml"), change("src/a.py", status="removed", patch=None))[0], "governance")

    def test_unmatched_and_unreadable_are_review(self):
        self.assertEqual(cls(change("src/app.py")), ("review", ["review"]))
        self.assertEqual(cls(change("docs/big.md", patch=None)), ("review", ["review", "docs"]))
        self.assertEqual(cls(), ("review", ["review"]))

    def test_repo_classes(self):
        self.assertEqual(cls(change("src/billing/pay.ts"))[0], "payments")
        self.assertEqual(cls(change("src/q.py", patch="@@ -0,0 +1 @@\n+import elastic"), change("src/app.py")), ("search", ["search", "review"]))

    def test_kit_bump_is_labelled_but_governance_wins_o1(self):
        old, new = "1" * 40, KIT
        workflow = change(".github/workflows/verify.yml",
                          patch=f"@@ -3 +3 @@\n-    uses: yudduy/agent-kit/.github/workflows/verify.yml@{old}\n+    uses: yudduy/agent-kit/.github/workflows/verify.yml@{new}")
        policy = change(".agent-kit/policy.yml", patch=f"@@ -2 +2 @@\n-kit_ref: {old}\n+kit_ref: {new}")
        self.assertEqual(cls(workflow, policy), ("governance", ["governance", "kit-bump"]))
        other = change(".github/workflows/verify.yml", patch="@@ -3 +3 @@\n-    run: a\n+    run: b")
        self.assertEqual(cls(other), ("governance", ["governance"]))


if __name__ == "__main__":
    unittest.main()
