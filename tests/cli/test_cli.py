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
        self.assertIn("required_checks: [{name: agent-learning-check, app: github-actions}]", files[".agent-kit/policy.yml"])
        workflow = files[".github/workflows/agent-learning-check.yml"]
        self.assertIn("name: agent-learning-check", workflow)
        self.assertNotIn("if: contains", workflow, "a skipped job would fail the gate's required check")
        self.assertIn("learn/", workflow)
        self.assertIn("governance", workflow)

    def test_codeowners_lines_are_appended_to_the_existing_file_and_other_files_kept(self):
        repo = checkout(**{"CODEOWNERS": "* @someone\n/.github/ @yudduy\n", "docs/features/README.md": "mine\n"})
        writes = {p: (a, t) for p, a, t in onboard.plan(repo, onboard.render("yudduy/shop", SHA))}
        action, text = writes["CODEOWNERS"]
        self.assertEqual(action, "append")
        self.assertTrue(text.startswith("* @someone\n/.github/ @yudduy\n/.agent-kit/ @yudduy\n"))
        self.assertEqual(text.count("/.github/ @yudduy"), 1)
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


class SyncPins(unittest.TestCase):
    def test_bump_moves_every_kit_pin_in_templated_files(self):
        files = onboard.render("yudduy/shop", SHA)
        repo = checkout(**files)
        changed = onboard.bump(repo, NEW)
        self.assertIn(".github/workflows/agent-learning-check.yml", changed)
        for rel in files:
            self.assertNotIn(SHA, (repo / rel).read_text(), rel)
        self.assertIn("ref: " + NEW, (repo / ".github/workflows/agent-learning-check.yml").read_text())


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
