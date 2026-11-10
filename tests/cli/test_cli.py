import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cli import onboard
from cli.__main__ import main

SHA = "a" * 40
NEW = "b" * 40


def checkout(**files):
    root = Path(tempfile.mkdtemp())
    for rel, text in {"Dockerfile": "FROM scratch\n", **files}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


class Render(unittest.TestCase):
    def test_templates_get_the_full_kit_sha_and_no_placeholder_is_left(self):
        files = onboard.render("yudduy/shop", SHA)
        self.assertIn("kit_ref: " + SHA, files[".agent-kit/policy.yml"])
        self.assertIn("ref: " + SHA, files[".github/workflows/agent-learning-check.yml"])
        self.assertFalse([p for p, t in files.items() if "{{KIT_REF}}" in t or "{{REPO}}" in t or "{{APP}}" in t])
        self.assertIn("/.agent-kit/ @yudduy", files[".github/CODEOWNERS"])
        self.assertIn("docs/features/README.md", files)
        self.assertIn(".cursor/environment.json", files)

    def test_refuses_a_repo_that_already_has_agent_kit(self):
        repo = checkout(**{".agent-kit/policy.yml": "version: 1\n"})
        with self.assertRaises(SystemExit) as stop:
            onboard.plan(repo, onboard.render("yudduy/shop", SHA))
        self.assertIn("--force", str(stop.exception))
        writes = {p: a for p, a, _ in onboard.plan(repo, onboard.render("yudduy/shop", SHA), force=True)}
        self.assertEqual(writes[".agent-kit/policy.yml"], "overwrite")

    def test_a_repo_without_a_dockerfile_is_onboarded_and_the_run_is_told_to_add_one(self):
        repo = checkout()
        (repo / "Dockerfile").unlink()
        writes = onboard.plan(repo, onboard.render("yudduy/shop", SHA))
        self.assertIn(".agent-kit/policy.yml", [p for p, _, _ in writes])
        self.assertIn("Dockerfile", onboard.JOURNEYS_PROMPT)

    def test_agents_md_gets_the_automation_marker_once(self):
        marker = "<!-- agent-kit:automation v1 seer:off cursor-sentry:off -->"
        writes = {p: (a, t) for p, a, t in onboard.plan(checkout(), onboard.render("yudduy/shop", SHA))}
        self.assertEqual(writes["AGENTS.md"][0], "create")
        self.assertIn(marker, writes["AGENTS.md"][1])
        repo = checkout(**{"AGENTS.md": "# Rules\nBe careful.\n"})
        action, text = {p: (a, t) for p, a, t in onboard.plan(repo, onboard.render("yudduy/shop", SHA))}["AGENTS.md"]
        self.assertEqual(action, "append")
        self.assertTrue(text.startswith("# Rules\nBe careful.\n"))
        self.assertEqual(text.count(marker), 1)
        repo = checkout(**{"AGENTS.md": "# Rules\n" + marker + "\n"})
        self.assertNotIn("AGENTS.md", [p for p, a, _ in onboard.plan(repo, onboard.render("yudduy/shop", SHA)) if a != "keep"])

    def test_learning_check_is_required_and_runs_on_every_pr(self):
        files = onboard.render("yudduy/shop", SHA)
        self.assertRegex(files[".agent-kit/policy.yml"], r"required_checks: \[.*\{name: agent-learning-check, app: github-actions\}")
        workflow = files[".github/workflows/agent-learning-check.yml"]
        self.assertIn("name: agent-learning-check", workflow)
        self.assertNotIn("if: contains", workflow, "a skipped job would fail the gate's required check")
        self.assertIn("learn/", workflow)
        self.assertIn("governance", workflow)

    def test_learning_proof_is_required_and_runs_on_every_pr(self):
        files = onboard.render("yudduy/shop", SHA)
        self.assertIn("{name: learning-proof, app: github-actions}", files[".agent-kit/policy.yml"])
        workflow = files[".github/workflows/agent-learning-check.yml"]
        job = workflow[workflow.index("name: learning-proof"):]
        self.assertIn("learning.proof", job)
        self.assertIn("github.event.pull_request.base.sha", job)
        self.assertIn("github.event.pull_request.head.sha", job)
        self.assertNotIn("write", job.split("steps:")[0], "the job that runs PR code gets read permissions only")
        self.assertNotIn("secrets.", job)

    def test_feature_docs_automation_renders_with_the_recursion_guard(self):
        doc = onboard.render("yudduy/shop", SHA)[".agent-kit/automations/feature-docs.md"]
        self.assertIn("Pull request merged", doc)
        self.assertIn("yudduy/shop", doc)
        self.assertIn("agent-kit/docs-", doc)
        self.assertIn("only touches `docs/features/`", doc)
        self.assertIn("Stop", doc)

    def test_codeowners_lines_are_appended_to_the_existing_file_and_other_files_kept(self):
        repo = checkout(**{"CODEOWNERS": "* @someone\n/.github/ @yudduy\n", "docs/features/README.md": "mine\n"})
        writes = {p: (a, t) for p, a, t in onboard.plan(repo, onboard.render("yudduy/shop", SHA))}
        action, text = writes["CODEOWNERS"]
        self.assertEqual(action, "append")
        self.assertTrue(text.startswith("* @someone\n/.github/ @yudduy\n\n" + onboard.OWNERS_START + "\n/.agent-kit/ @yudduy\n"))
        self.assertNotIn(".github/CODEOWNERS", writes)
        self.assertEqual(writes["docs/features/README.md"][0], "keep")


class DryRun(unittest.TestCase):
    def test_dry_run_prints_the_plan_and_never_calls_gh_or_git_push(self):
        real = subprocess.run

        def guarded(args, **kw):
            if args[0] != "git" or "rev-parse" not in args:
                raise AssertionError("dry run must not run %s" % args)
            return real(args, **kw)

        out = io.StringIO()
        with mock.patch("subprocess.run", side_effect=guarded), redirect_stdout(out):
            self.assertEqual(main(["init", "--dry-run", "yudduy/agent-kit"]), 0)
        text = out.getvalue()
        self.assertIn(".agent-kit/policy.yml", text)
        self.assertIn("Draft PR: Add agent kit onboarding", text)
        self.assertIn("agent-kit/onboard", text)


    def test_dry_run_prints_the_feature_docs_setup_step(self):
        out = io.StringIO()
        with redirect_stdout(out):
            main(["init", "--dry-run", "--ref", SHA, "yudduy/shop"])
        self.assertIn(".agent-kit/automations/feature-docs.md", out.getvalue())
        self.assertIn("Cursor Automation", out.getvalue())


class Journeys(unittest.TestCase):
    def test_no_cursor_key_prints_the_next_step(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(out):
            self.assertIsNone(onboard.start_journeys("yudduy/shop"))
        self.assertIn("agent-kit journeys yudduy/shop", out.getvalue())

    def test_run_commits_to_the_onboarding_branch_without_a_new_pr(self):
        client = mock.Mock()
        client.create.return_value = {"id": "bc-1", "url": "https://cursor.com/agents/bc-1"}
        with redirect_stdout(io.StringIO()):
            onboard.start_journeys("yudduy/shop", client=client)
        args, kwargs = client.create.call_args
        self.assertEqual(args[3:5], ("yudduy/shop", "agent-kit/onboard"))
        self.assertEqual(kwargs, {"work_on_branch": True, "auto_pr": False})
        self.assertIn("create-verification-skill", args[2])
        self.assertIn("data_assertions", args[2])


class Sync(unittest.TestCase):
    def test_bump_changes_only_sha_pins(self):
        caller = "jobs:\n  verify:\n    uses: yudduy/agent-kit/.github/workflows/verify.yml@%s\n    with: {x: 1}\n" % SHA
        repo = checkout(**{".agent-kit/policy.yml": "version: 1\nkit_ref: %s\napprovers: [yudduy]\n" % SHA,
                           ".github/workflows/agent-verify.yml": caller,
                           ".github/workflows/other.yml": "uses: actions/checkout@%s\n" % SHA})
        self.assertEqual(sorted(onboard.bump(repo, NEW)), [".agent-kit/policy.yml", ".github/workflows/agent-verify.yml"])
        self.assertEqual((repo / ".github/workflows/agent-verify.yml").read_text(), caller.replace(SHA, NEW))
        self.assertIn("kit_ref: " + NEW, (repo / ".agent-kit/policy.yml").read_text())
        self.assertIn(SHA, (repo / ".github/workflows/other.yml").read_text())


class OpenBump(unittest.TestCase):
    def test_an_open_bump_pr_is_found_and_sync_skips(self):
        prs = ('[{"headRefName": "cursor/fix-x", "url": "u1", "isCrossRepository": false}, '
               '{"headRefName": "agent-kit/bump-%s", "url": "u2", "isCrossRepository": false}]' % SHA[:12])
        with mock.patch.object(onboard, "run", return_value=prs) as gh:
            self.assertEqual(onboard.open_bump("yudduy/shop"), "u2")
        self.assertEqual(gh.call_args[0][:6], ("gh", "pr", "list", "-R", "yudduy/shop", "--state"))
        for listing in ('[]', '[{"headRefName": "agent-kit/onboard", "url": "u3", "isCrossRepository": false}, '
                              '{"headRefName": "x/agent-kit/bump-1", "url": "u4", "isCrossRepository": false}]',
                        '[{"headRefName": "agent-kit/bump-from-a-fork", "url": "u5", "isCrossRepository": true}]'):
            with mock.patch.object(onboard, "run", return_value=listing):
                self.assertIsNone(onboard.open_bump("yudduy/shop"))
        out = io.StringIO()
        with mock.patch.object(onboard, "open_bump", return_value="u2"), mock.patch.object(onboard, "clone") as clone, redirect_stdout(out):
            self.assertEqual(onboard.sync("yudduy/shop", NEW), 0)
        clone.assert_not_called()
        self.assertIn("u2", out.getvalue())

    def test_kit_bump_workflow_syncs_every_registered_repo_daily(self):
        workflow = (onboard.KIT / "templates/agent-control/.github/workflows/kit-bump.yml").read_text()
        self.assertIn("schedule:", workflow)
        self.assertIn("agent-kit/repos.json", workflow)
        self.assertIn("sync", workflow)
        self.assertIn("AGENT_KIT_BUMP_TOKEN", workflow)
        self.assertNotIn("merge-gate", workflow)


class CodeOwners(unittest.TestCase):
    def owners(self, repo):
        return {p: t for p, a, t in onboard.plan(repo, onboard.render("yudduy/shop", SHA)) if a != "keep"}.get("CODEOWNERS")

    def test_the_kit_block_comes_after_rules_that_would_override_it(self):
        repo = checkout(**{"CODEOWNERS": "/.github/ @yudduy\n* @other-team\n"})
        lines = self.owners(repo).splitlines()
        self.assertEqual(lines[:2], ["/.github/ @yudduy", "* @other-team"])
        self.assertGreater(len(lines) - 1 - lines[::-1].index("/.github/ @yudduy"), lines.index("* @other-team"),
                           "the kit's /.github/ rule is the last match, after the catch-all")

    def test_rerunning_moves_the_block_to_the_end_once(self):
        repo = checkout(**{"CODEOWNERS": "* @other-team\n"})
        (repo / "CODEOWNERS").write_text(self.owners(repo) + "/docs/ @writers\n")
        text = self.owners(repo)
        self.assertEqual(text.count("/.agent-kit/ @yudduy"), 1)
        self.assertTrue(text.rstrip().endswith(onboard.OWNERS_END))
        self.assertLess(text.index("/docs/ @writers"), text.index("/.agent-kit/ @yudduy"))


class SyncPins(unittest.TestCase):
    def test_bump_moves_every_kit_pin_in_templated_files(self):
        files = onboard.render("yudduy/shop", SHA)
        repo = checkout(**files)
        changed = onboard.bump(repo, NEW)
        self.assertIn(".github/workflows/agent-learning-check.yml", changed)
        for rel in files:
            self.assertNotIn(SHA, (repo / rel).read_text(), rel)
        self.assertIn("ref: " + NEW, (repo / ".github/workflows/agent-learning-check.yml").read_text())


class QuotedPins(unittest.TestCase):
    def test_bump_handles_quoted_kit_ref_and_uses(self):
        files = onboard.render("yudduy/shop", SHA)
        files[".agent-kit/policy.yml"] = files[".agent-kit/policy.yml"].replace("kit_ref: " + SHA, 'kit_ref: "%s"' % SHA)
        files[".github/workflows/caller.yml"] = 'jobs:\n  v:\n    uses: "yudduy/agent-kit/.github/workflows/verify.yml@%s"\n' % SHA
        repo = checkout(**files)
        onboard.bump(repo, NEW)
        for rel in files:
            self.assertNotIn(SHA, (repo / rel).read_text(), rel)
        self.assertIn('kit_ref: "%s"' % NEW, (repo / ".agent-kit/policy.yml").read_text())


class OtherCommands(unittest.TestCase):
    def test_doctor_and_verify_explain_when_their_modules_are_missing(self):
        with mock.patch.dict("sys.modules", {"doctor": None, "doctor.doctor": None, "verify": None, "verify.local_run": None}):
            for argv in (["doctor"], ["verify", "--sha", "a" * 40, "--scenario", "save-profile"]):
                with self.assertRaises(SystemExit) as stop:
                    main(argv)
                self.assertIn("not in this kit checkout", str(stop.exception))

    def test_doctor_passes_its_arguments_through(self):
        fake = mock.Mock()
        fake.main.return_value = 0
        package = mock.Mock(doctor=fake)
        with mock.patch.dict("sys.modules", {"doctor": package, "doctor.doctor": fake}):
            self.assertEqual(main(["doctor", ".", "--env-file", "env.txt"]), 0)
        fake.main.assert_called_once_with([".", "--env-file", "env.txt"])

    def test_verify_reruns_one_scenario_on_a_head_commit(self):
        fake = mock.Mock()
        fake.main.return_value = 0
        package = mock.Mock(local_run=fake)
        with mock.patch.dict("sys.modules", {"verify": package, "verify.local_run": fake}):
            self.assertEqual(main(["verify", "--sha", "b" * 40, "--scenario", "save-profile", "--base", "c" * 40,
                                   "--isolation", "host"]), 0)
        argv = fake.main.call_args[0][0]
        self.assertEqual(argv[:6], ["--repo", ".", "--head", "b" * 40, "--base", "c" * 40])
        self.assertEqual(argv[argv.index("--scenario") + 1], "save-profile")
        self.assertIn("--out", argv)
        self.assertEqual(argv[-2:], ["--isolation", "host"])


if __name__ == "__main__":
    unittest.main()
