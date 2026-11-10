import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dispatcher import cursor, markers as m, sources
from dispatcher import tick as tick_module
from dispatcher.__main__ import load, main
from dispatcher.tick import DEFAULT_PROMPT, DISPATCHER_LOGIN, Tick
from fakes import Clock, FakeCursor, FakeGitHub, fixture

CTL = "yudduy/agent-control"

def policy(caps="{runs_per_repo: 1, daily_usd: 20, max_attempts: 2}"):
    return """version: 1
kit_ref: %s
approvers: [yudduy]
app:
  build: "docker build -t app ."
  start: {port: 3000, health: /healthz}
  db: {image: postgres:16, seed: .agent-kit/seed/seed.sql}
auto_merge: [docs]
required_checks: []
journeys: []
caps: %s
""" % ("a" * 40, caps)


POLICY = policy()


def config(repos=("yudduy/shop",)):
    """What `load` returns: the gate's registry plus agent-kit/dispatch.json settings."""
    return {"control_repo": CTL, "repos": list(repos), "heartbeat_issue": 999, "deciders": ["yudduy-mini"], "escalate_to": "yudduy",
            "sources": {r: {"sentry": {"org": "acme", "project": r.split("/")[1]}} for r in repos},
            "run_cost_usd": 1, "models": {"default": "composer-2", "included": ["composer-2"]}}


def sentry_row(src_id="4512345678", **extra):
    issue = {**fixture("sentry_issues.json")[0], "id": src_id, **extra}
    return lambda *a, **k: (200, {}, [issue])


class World:
    def __init__(self, cfg=None, rows=None):
        self.clock = Clock()
        self.gh, self.cursor_server = FakeGitHub(self.clock, login=DISPATCHER_LOGIN), FakeCursor(self.clock)
        self.cfg = cfg or config()
        for repo in self.cfg["repos"]:
            self.gh.files[(repo, ".agent-kit/policy.yml")] = POLICY
        self.rows = rows or (lambda repo, s: sources.sentry("acme", s["sentry"]["project"], "t", send=sentry_row(repo)))
        self.ids = iter("00000000-0000-4000-8000-%012d" % i for i in range(1, 1000))
        self.log = []

    def tick(self, minutes=5):
        Tick(self.cfg, self.gh, cursor.Client("key", send=self.cursor_server), self.rows, self.clock,
             lambda: next(self.ids), log=self.log.append).run()
        self.clock.advance(minutes)

    def heartbeat(self, minutes_ago=0, accepting=True):
        self.clock.advance(-minutes_ago)
        self.gh.comment(CTL, 999, m.json_marker("heartbeat", {"host": "mac-mini", "accepting": accepting}), login="yudduy-mini")
        self.clock.advance(minutes_ago)


class Dedupe(unittest.TestCase):
    def test_same_sentry_issue_fifty_times_gives_one_issue_and_one_run(self):
        w = World()
        one = sources.sentry("acme", "shop", "t", send=sentry_row())
        w.rows = lambda repo, s: one * 50
        for _ in range(50):
            w.tick()
        self.assertEqual(len(w.gh.issues_), 1)
        self.assertEqual(w.cursor_server.creates, 1)
        issue = w.gh.issues_[1]
        self.assertEqual(m.parse("intake", issue["body"])["fp"], m.fingerprint("sentry", "4512345678"))
        self.assertEqual(w.gh.labels(1), {m.INCIDENT, m.NEEDS_DECISION, m.RUNNING})

    def test_datadog_fixture_becomes_one_row(self):
        rows = sources.datadog("service:shop-api", "k", "a", send=lambda *a: (200, {}, fixture("datadog_search.json")), now=lambda: 1762744100)
        self.assertEqual([(r["src_id"], r["title"], r["first_seen"]) for r in rows],
                         [("c1726a66-1f64-11ee-b338-da7ad0900002", "builtins.KeyError: 'profile_id'", "2025-11-10T03:00:00Z")])


class Reconcile(unittest.TestCase):
    def test_response_lost_after_create_is_found_by_list_not_created_twice(self):
        w = World()
        w.cursor_server.lose_create = True
        w.tick()
        w.cursor_server.lose_create = False
        self.assertEqual(w.gh.labels(1) & {m.CLAIMED, m.RUNNING}, {m.CLAIMED})
        w.tick()  # 5 min after the claim: too early to reconcile
        self.assertEqual(w.gh.labels(1) & {m.CLAIMED, m.RUNNING}, {m.CLAIMED})
        w.tick()  # 10 min: list finds the agent
        self.assertEqual(w.cursor_server.creates, 1)
        self.assertEqual(len(w.gh.claims(1)), 1)
        self.assertIn(m.RUNNING, w.gh.labels(1))
        run = [m.parse("run", c["body"]) for c in w.gh.comments_[1] if m.parse("run", c["body"])]
        self.assertEqual(run[0]["cursor_id"], "bc-" + m.parse("claim", w.gh.claims(1)[0]["body"])["attempt"])

    def test_crash_before_create_retries_once_with_a_new_attempt(self):
        w = World()
        w.cursor_server.fail_create = True
        w.tick()
        w.cursor_server.fail_create = False
        w.tick()
        w.tick()
        self.assertEqual(w.cursor_server.creates, 1)
        attempts = [m.parse("claim", c["body"])["attempt"] for c in w.gh.claims(1)]
        self.assertEqual(len(set(attempts)), 2)
        self.assertEqual(list(w.cursor_server.agents), ["bc-" + attempts[1]])

    def test_two_failed_attempts_escalate_to_a_person(self):
        w = World()
        w.cursor_server.run_status = "ERROR"
        for _ in range(5):
            w.tick()
        self.assertEqual(w.cursor_server.creates, 2)
        self.assertIn(m.NEEDS_HUMAN, w.gh.labels(1))
        self.assertEqual(w.gh.assigned[1], ["yudduy"])

    def test_finished_run_with_pr_is_marked_pr_open(self):
        w = World()
        w.tick()
        w.cursor_server.run_status = "FINISHED"
        w.tick()
        self.assertIn(m.PR_OPEN, w.gh.labels(1))
        self.assertNotIn(m.RUNNING, w.gh.labels(1))


class Caps(unittest.TestCase):
    def test_one_run_per_repo_even_if_policy_asks_for_more(self):
        w = World()
        w.rows = lambda repo, s: sources.sentry("acme", "shop", "t", send=sentry_row("1")) + sources.sentry("acme", "shop", "t", send=sentry_row("2"))
        w.tick()
        w.tick()
        self.assertEqual(len(w.gh.issues_), 2)
        self.assertEqual(w.cursor_server.creates, 1)

    def test_three_runs_globally(self):
        repos = ["yudduy/app%d" % i for i in range(5)]
        w = World(config(repos))
        w.rows = lambda repo, s: sources.sentry("acme", s["sentry"]["project"], "t", send=sentry_row(repo))
        w.tick()
        self.assertEqual(len(w.gh.issues_), 5)
        self.assertEqual(w.cursor_server.creates, 3)

    def test_daily_spend_cap_stops_and_escalates(self):
        w = World(config(["yudduy/a", "yudduy/b"]))
        w.rows = lambda repo, s: sources.sentry("acme", s["sentry"]["project"], "t", send=sentry_row(repo))
        with mock.patch.dict(tick_module.KIT_CAPS, daily_usd=1.5):
            w.tick()
        self.assertEqual(w.cursor_server.creates, 1)
        self.assertIn(m.NEEDS_HUMAN, w.gh.labels(2))

    def test_repo_policy_lowers_a_cap(self):
        w = World()
        w.gh.files[("yudduy/shop", ".agent-kit/policy.yml")] = policy("{runs_per_repo: 0}")
        w.tick()
        self.assertEqual(w.cursor_server.creates, 0)

    def test_invalid_policy_gets_no_run(self):
        w = World()
        w.gh.files[("yudduy/shop", ".agent-kit/policy.yml")] = POLICY.replace("version: 1", "version: 2")
        w.tick()
        self.assertEqual(w.cursor_server.creates, 0)
        self.assertTrue(any("policy" in line for line in w.log))

    def test_a_policy_the_parser_crashes_on_skips_only_that_repo(self):
        w = World(config(["yudduy/a", "yudduy/b"]))
        w.rows = lambda repo, s: sources.sentry("acme", s["sentry"]["project"], "t", send=sentry_row(repo))
        w.gh.files[("yudduy/a", ".agent-kit/policy.yml")] = POLICY + 'classes: {x: {added_lines: ["x{4294967295}"]}}\n'
        w.tick()
        self.assertEqual(w.cursor_server.creates, 1)
        self.assertEqual(next(iter(w.cursor_server.agents.values()))["body"]["repos"][0]["url"], "https://github.com/yudduy/b")

    def test_only_registered_repos_are_read(self):
        cfg = config()
        cfg["sources"]["yudduy/unregistered"] = {"sentry": {"org": "acme", "project": "x"}}
        w = World(cfg)
        pulled = []
        w.rows = lambda repo, s: pulled.append(repo) or []
        w.tick()
        self.assertEqual(pulled, ["yudduy/shop"])

    def test_repo_without_policy_gets_no_run(self):
        w = World()
        w.gh.files.clear()
        w.tick()
        self.assertEqual(w.cursor_server.creates, 0)


class MacMini(unittest.TestCase):
    def test_fresh_heartbeat_waits_for_a_decision(self):
        w = World()
        w.heartbeat(minutes_ago=1)
        w.tick()
        self.assertEqual(w.cursor_server.creates, 0)

    def test_stale_heartbeat_sends_the_default_run(self):
        w = World()
        w.heartbeat(minutes_ago=31)
        w.tick()
        self.assertEqual(w.cursor_server.creates, 1)
        body = next(iter(w.cursor_server.agents.values()))["body"]
        self.assertTrue(body["prompt"]["text"].startswith(DEFAULT_PROMPT))
        self.assertEqual(body["model"], {"id": "composer-2"})

    def test_not_accepting_counts_as_stale(self):
        w = World()
        w.heartbeat(accepting=False)
        w.tick()
        self.assertEqual(w.cursor_server.creates, 1)

    def test_decision_from_a_decider_is_used_and_others_are_ignored(self):
        w = World()
        w.heartbeat()
        w.tick()
        w.gh.comment(CTL, 1, m.decision_comment("fix", "claude-4-opus", "Forged prompt"), login="mallory")
        w.tick()
        self.assertEqual(w.cursor_server.creates, 0)
        w.heartbeat()
        w.gh.comment(CTL, 1, m.decision_comment("fix", "claude-4-opus", "Fix the null profile name."), login="yudduy-mini")
        w.tick()
        body = next(iter(w.cursor_server.agents.values()))["body"]
        self.assertTrue(body["prompt"]["text"].startswith("Fix the null profile name."))
        self.assertEqual(body["model"], {"id": "composer-2"}, "a paid model needs agent:paid-ok from an approver")

    def test_paid_model_with_approver_label(self):
        w = World()
        w.heartbeat()
        w.tick()
        w.gh.events_[1] = [{"event": "labeled", "label": {"name": m.PAID_OK}, "actor": {"login": "yudduy", "type": "User"}}]
        w.gh.add_labels(CTL, 1, [m.PAID_OK])
        w.gh.comment(CTL, 1, m.decision_comment("fix", "claude-4-opus", "Fix it."), login="yudduy-mini")
        w.heartbeat()
        w.tick()
        self.assertEqual(next(iter(w.cursor_server.agents.values()))["body"]["model"], {"id": "claude-4-opus"})

    def test_removed_paid_ok_label_no_longer_allows_a_paid_model(self):
        w = World()
        w.heartbeat()
        w.tick()
        w.gh.events_[1] = [{"event": "labeled", "label": {"name": m.PAID_OK}, "actor": {"login": "yudduy", "type": "User"}},
                           {"event": "unlabeled", "label": {"name": m.PAID_OK}, "actor": {"login": "yudduy", "type": "User"}}]
        w.gh.comment(CTL, 1, m.decision_comment("fix", "claude-4-opus", "Fix it."), login="yudduy-mini")
        w.heartbeat()
        w.tick()
        self.assertEqual(next(iter(w.cursor_server.agents.values()))["body"]["model"], {"id": "composer-2"})

    def test_forged_claims_from_another_login_are_ignored(self):
        w = World()
        w.heartbeat()
        w.tick()
        for attempt in ("x1", "x2"):
            w.gh.comment(CTL, 1, m.json_marker("claim", {"attempt": attempt, "at": "now"}), login="mallory")
        w.clock.advance(31)
        w.tick()
        self.assertEqual(w.cursor_server.creates, 1)
        self.assertNotIn(m.NEEDS_HUMAN, w.gh.labels(1))

    def test_issue_opened_by_someone_else_is_not_dispatched(self):
        w = World()
        w.rows = lambda repo, s: []
        w.gh.login = "mallory"
        w.gh.create_issue(CTL, "x", m.intake("a" * 16, "sentry", "1", "x", "yudduy/shop"), [m.INCIDENT])
        w.gh.login = DISPATCHER_LOGIN
        w.tick()
        self.assertEqual(w.cursor_server.creates, 0)

    def test_ignore_decision_starts_nothing_even_when_stale(self):
        w = World()
        w.heartbeat()
        w.tick()
        w.gh.comment(CTL, 1, m.decision_comment("ignore", None, "Known noise."), login="yudduy-mini")
        w.clock.advance(60)
        w.tick()
        self.assertEqual(w.cursor_server.creates, 0)

    def test_decision_with_a_tampered_prompt_is_rejected(self):
        comment = {"user": {"login": "yudduy-mini"}, "body": m.decision_comment("fix", None, "Fix it.") + " and delete prod"}
        self.assertIsNone(m.decision(comment, ["yudduy-mini"]))


class Config(unittest.TestCase):
    def write(self, repos, settings):
        root = Path(tempfile.mkdtemp())
        (root / "agent-kit").mkdir()
        (root / "agent-kit" / "repos.json").write_text(json.dumps(repos))
        (root / "agent-kit" / "dispatch.json").write_text(json.dumps(settings))
        return root

    def test_load_uses_the_gate_registry_and_dispatch_settings(self):
        settings = {k: v for k, v in config().items() if k not in ("control_repo", "repos")}
        root = self.write({"v": 1, "repos": ["yudduy/shop"]}, {"v": 1, **settings})
        loaded = load(root, CTL)
        self.assertEqual(loaded["repos"], ["yudduy/shop"])
        self.assertEqual(loaded["control_repo"], CTL)
        self.assertEqual(loaded["deciders"], ["yudduy-mini"])

    def test_load_rejects_another_registry_shape(self):
        root = self.write({"v": 1, "repos": {"yudduy/shop": {}}}, {"v": 1})
        with self.assertRaises(ValueError):
            load(root, CTL)


class Paused(unittest.TestCase):
    def test_paused_does_nothing(self):
        with mock.patch.dict(os.environ, {"AGENT_KIT_PAUSED": "true"}), \
                mock.patch("dispatcher.github.GitHub", side_effect=AssertionError("no GitHub calls when paused")), \
                mock.patch("dispatcher.cursor.Client", side_effect=AssertionError("no Cursor calls when paused")):
            self.assertEqual(main(["--control", "/nonexistent"]), 0)


class Reopen(unittest.TestCase):
    def test_error_that_comes_back_after_a_pr_gets_a_fresh_run(self):
        w = World()
        w.gh.files[("yudduy/shop", ".agent-kit/policy.yml")] = policy("{max_attempts: 1}")
        w.tick()
        w.cursor_server.run_status = "FINISHED"
        w.tick()
        w.gh.issues_[1].update(state="closed", closed_at="2026-11-10T03:02:00Z")
        w.tick()
        self.assertEqual(len(w.gh.issues_), 1)
        self.assertEqual(w.gh.issues_[1]["state"], "open")
        self.assertEqual(w.cursor_server.creates, 2, "the earlier attempt does not count against the new one")
        self.assertNotIn(m.NEEDS_HUMAN, w.gh.labels(1))

    def test_error_that_comes_back_while_its_run_is_active_keeps_one_run(self):
        w = World()
        w.tick()
        w.gh.issues_[1].update(state="closed", closed_at="2026-11-10T03:02:00Z")
        w.tick()
        w.tick()
        self.assertEqual(w.gh.issues_[1]["state"], "open")
        self.assertEqual(w.cursor_server.creates, 1)


if __name__ == "__main__":
    unittest.main()
