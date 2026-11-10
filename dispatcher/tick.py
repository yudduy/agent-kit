"""One dispatcher tick (D4): intake errors, then start, reconcile or follow each incident's Cursor run.

Run state lives only in agent-control issues (C3). The workflow's global concurrency group makes this the single writer.
"""
from datetime import datetime, timedelta, timezone

from dispatcher import cursor as cursor_api
from dispatcher import markers as m
from gate import policy

# Kit caps (D4). A repo's policy `caps` can lower runs_per_repo, daily_usd and max_attempts, never raise them.
KIT_CAPS = {"runs_per_repo": 1, "global_runs": 3, "daily_usd": 20, "max_attempts": 2}
# Issue writes use dispatch.yml's own token, whose author is this login; markers from anyone else are ignored.
DISPATCHER_LOGIN = "github-actions[bot]"

DEFAULT_PROMPT = (
    "Reproduce this production error in a fresh environment using the repo's verify skill (.cursor/skills/verify-*). "
    "If you can reproduce it, fix it and add a regression test that fails before your fix and passes after, then open a PR. "
    "If you cannot reproduce it or cannot fix it safely, do not open a PR; reply with a triage: what you tried, "
    "what you found, and what a person should check next."
)
CLAIM_TIMEOUT = timedelta(minutes=10)
HEARTBEAT_STALE = timedelta(minutes=30)


def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def labels_of(issue):
    return {label["name"] for label in issue.get("labels", [])}


class Tick:
    def __init__(self, config, gh, cursor, pull, now, new_id, log=print, reader=None):
        """`config` comes from `dispatcher.__main__.load`. `gh` writes agent-control issues; `reader` (default `gh`)
        reads policy files from onboarded repos."""
        self.config, self.gh, self.cursor, self.pull, self.log = config, gh, cursor, pull, log
        self.reader = reader or gh
        self.now, self.new_id = now, new_id
        self.ctl, self.me, self.kit = config["control_repo"], DISPATCHER_LOGIN, KIT_CAPS
        self.cost = config["run_cost_usd"]

    def run(self):
        self.issues = self.gh.issues(self.ctl, m.INCIDENT, state="all")
        self.intake()
        self._comments = {}
        today = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.active, self.spent = {}, {}
        for issue in self.issues:
            repo = self.repo_of(issue)
            if issue["state"] == "open" and labels_of(issue) & {m.CLAIMED, m.RUNNING}:
                self.active[repo] = self.active.get(repo, 0) + 1
            if ts(issue["updated_at"]) >= today:
                claims_today = [c for c in self.claims(issue, since_reopen=False) if ts(c["created_at"]) >= today]
                self.spent[repo] = self.spent.get(repo, 0) + self.cost * len(claims_today)
        self.stale = self.heartbeat_stale()
        self.policies = {}
        for issue in sorted(self.issues, key=lambda i: i["number"]):
            if issue["state"] == "open" and not labels_of(issue) & m.DONE and self.repo_of(issue) in self.config["repos"]:
                self.step(issue)

    # Intake (C3): one issue per fingerprint, reopened when a closed error comes back.
    def intake(self):
        by_fp = {}
        self.issues = [i for i in self.issues if self.mine(i)]  # an intake marker counts only in an issue the dispatcher opened
        for issue in self.issues:
            fields = m.parse("intake", issue.get("body"))
            if fields:
                by_fp[fields["fp"]] = issue
        for repo in self.config["repos"]:
            try:
                rows = self.pull(repo, self.config["sources"].get(repo, {}))
            except Exception as error:  # one broken source must not stop the tick
                self.log("Could not read errors for %s: %s" % (repo, error))
                continue
            for row in rows:
                fp = m.fingerprint(row["source"], row["src_id"])
                issue = by_fp.get(fp)
                if issue is None:
                    by_fp[fp] = issue = self.open_incident(repo, fp, row)
                    self.issues.append(issue)
                elif issue["state"] == "closed" and m.IGNORED not in labels_of(issue) and row["last_seen"] \
                        and ts(row["last_seen"]) > ts(issue["closed_at"]):
                    self.reopen(issue, row)

    def open_incident(self, repo, fp, row):
        body = "\n".join([m.intake(fp, row["source"], row["src_id"], row["first_seen"], repo),
                          "**Error:** %s" % row["title"], "**Where:** %s" % (row["where"] or "unknown"),
                          "**App:** %s" % repo, "**Seen:** %s times, first at %s" % (row["count"], row["first_seen"]),
                          "**Source:** %s" % row["link"]])
        title = "%s error in %s: %s" % (row["source"].capitalize(), repo.split("/")[-1], row["title"])
        return self.gh.create_issue(self.ctl, title[:250], body, [m.INCIDENT, m.NEEDS_DECISION])

    def reopen(self, issue, row):
        """Reopen with a fresh decision and attempt count, unless a run is still active: then keep following it (INV-8)."""
        number = issue["number"]
        self.gh.update_issue(self.ctl, number, state="open")
        issue["state"] = "open"
        if labels_of(issue) & {m.CLAIMED, m.RUNNING}:
            self.gh.comment(self.ctl, number, "This error came back (last seen %s) while its run is still active." % row["last_seen"])
            return
        for label in (m.PR_OPEN, m.TRIAGED, m.NEEDS_HUMAN, m.DECIDED):
            self.gh.remove_label(self.ctl, number, label)
        self.gh.add_labels(self.ctl, number, [m.NEEDS_DECISION])
        self.gh.comment(self.ctl, number, m.json_marker("reopen", {"last_seen": row["last_seen"]})
                        + "\nThis error came back after the issue was closed (last seen %s)." % row["last_seen"])
        issue["labels"] = [{"name": m.INCIDENT}, {"name": m.NEEDS_DECISION}]

    # Comment-derived state. Claim, run and reopen markers count only from the dispatcher's own login.
    def mine(self, item):
        return (item.get("user") or {}).get("login") == self.me

    def comments(self, issue, since_reopen=True):
        if issue["number"] not in self._comments:
            self._comments[issue["number"]] = self.gh.comments(self.ctl, issue["number"])
        everything = self._comments[issue["number"]]
        if not since_reopen:
            return everything
        reopens = [i for i, c in enumerate(everything) if self.mine(c) and m.parse("reopen", c["body"])]
        return everything[reopens[-1] + 1:] if reopens else everything

    def claims(self, issue, since_reopen=True):
        return [{**claim, "created_at": c["created_at"]} for c in self.comments(issue, since_reopen)
                if self.mine(c) and (claim := m.parse("claim", c["body"]))]

    def runs(self, issue):
        return {r["attempt"]: r["cursor_id"] for r in (m.parse("run", c["body"]) for c in self.comments(issue) if self.mine(c)) if r}

    def repo_of(self, issue):
        return (m.parse("intake", issue.get("body")) or {}).get("repo")

    def heartbeat_stale(self):
        beats = [c for c in self.gh.comments(self.ctl, self.config["heartbeat_issue"])
                 if (c.get("user") or {}).get("login") in self.config["deciders"] and m.parse("heartbeat", c["body"])]
        if not beats:
            return True
        latest = max(beats, key=lambda c: c["updated_at"])
        return self.now() - ts(latest["updated_at"]) > HEARTBEAT_STALE or not m.parse("heartbeat", latest["body"]).get("accepting")

    def caps(self, repo):
        """(caps, approvers) from the repo's policy on its default branch, or None when it is missing or invalid."""
        if repo not in self.policies:
            text = self.reader.file(repo, ".agent-kit/policy.yml")
            try:
                parsed = policy.parse(text) if text is not None else None
            except Exception as error:  # PolicyError, or a crash inside the parser: one repo must not stop the tick
                self.log("Skipping %s: its policy could not be read (%s)" % (repo, error))
                parsed = None
            self.policies[repo] = parsed and ({k: min(v, parsed.get("caps", {}).get(k, v)) for k, v in self.kit.items()},
                                              parsed["approvers"])
        return self.policies[repo]

    # Per-issue state machine.
    def step(self, issue):
        labels = labels_of(issue)
        if m.RUNNING in labels:
            self.follow(issue)
        elif m.CLAIMED in labels:
            self.reconcile(issue)
        else:
            self.maybe_start(issue)

    def maybe_start(self, issue):
        repo = self.repo_of(issue)
        loaded = self.caps(repo)
        if loaded is None:
            self.log("Skipping %s: no valid .agent-kit/policy.yml on its default branch." % repo)
            return
        caps, approvers = loaded
        if len(self.claims(issue)) >= caps["max_attempts"]:
            return self.escalate(issue, "%d attempts did not produce a fix or a triage" % caps["max_attempts"])
        decisions = [d for d in (m.decision(c, self.config["deciders"]) for c in self.comments(issue)) if d]
        if decisions:
            decision = decisions[-1]
            if decision["action"] == "ignore":
                return
            prompt, model = decision["prompt"], decision.get("model")
            if model not in self.config["models"]["included"] and not self.paid_ok(issue, approvers):
                model = self.config["models"]["default"]
        elif self.stale:
            prompt, model = DEFAULT_PROMPT, self.config["models"]["default"]
        else:
            return  # the Mac mini is up and has not decided yet
        if self.active.get(repo, 0) >= caps["runs_per_repo"] or sum(self.active.values()) >= self.kit["global_runs"]:
            return
        if sum(self.spent.values()) + self.cost > self.kit["daily_usd"] or self.spent.get(repo, 0) + self.cost > caps["daily_usd"]:
            return self.escalate(issue, "the daily spend cap is reached")
        self.start(issue, repo, prompt, model)

    def paid_ok(self, issue, approvers):
        """`agent:paid-ok` is on the issue now, and an approver (a user, not a bot) applied it last."""
        if m.PAID_OK not in labels_of(issue):
            return False
        applied = [e for e in self.gh.events(self.ctl, issue["number"]) if e.get("event") == "labeled" and e["label"]["name"] == m.PAID_OK]
        return bool(applied) and applied[-1]["actor"]["login"] in approvers and applied[-1]["actor"].get("type") == "User"

    def start(self, issue, repo, prompt, model):
        attempt = self.new_id()
        number = len(self.claims(issue)) + 1
        claim = self.gh.comment(self.ctl, issue["number"], m.json_marker("claim", {"attempt": attempt, "at": self.now().isoformat()})
                                + "\nAttempt %d claimed; starting a Cursor run." % number)
        self._comments[issue["number"]].append(claim)
        self.gh.add_labels(self.ctl, issue["number"], [m.CLAIMED])
        self.active[repo] = self.active.get(repo, 0) + 1
        self.spent[repo] = self.spent.get(repo, 0) + self.cost
        fields = m.parse("intake", issue["body"])
        text = "%s\n\nContext:\n%s\n- Incident: https://github.com/%s/issues/%d\n- In the PR body, write `Fixes %s#%d` and `agent-kit fp:%s`." % (
            prompt, issue["body"].split("-->", 1)[1].strip(), self.ctl, issue["number"], self.ctl, issue["number"], fields["fp"])
        try:
            agent = self.cursor.create(attempt, "agent-kit %s %s" % (fields["fp"], attempt), text, repo, None, model)
        except cursor_api.Ambiguous as error:
            self.log("Cursor create for #%d is ambiguous (%s); the next tick after 10 min reconciles it." % (issue["number"], error))
            return
        except cursor_api.CursorError as error:
            self.gh.comment(self.ctl, issue["number"], "Attempt %d failed: Cursor refused the run (%s)." % (number, error.status))
            self.release(issue, repo)
            return
        self.record(issue, attempt, agent)

    def record(self, issue, attempt, agent):
        line = self.gh.comment(self.ctl, issue["number"], m.json_marker("run", {"attempt": attempt, "cursor_id": agent["id"]})
                               + "\nCursor run started: %s" % agent.get("url", agent["id"]))
        self._comments[issue["number"]].append(line)
        self.gh.add_labels(self.ctl, issue["number"], [m.RUNNING])
        self.gh.remove_label(self.ctl, issue["number"], m.CLAIMED)

    def release(self, issue, repo):
        for label in (m.CLAIMED, m.RUNNING):
            self.gh.remove_label(self.ctl, issue["number"], label)
        self.active[repo] = self.active.get(repo, 1) - 1

    def reconcile(self, issue):
        """INV-8: a claim with no run line is looked up in Cursor before any retry."""
        runs, claims = self.runs(issue), self.claims(issue)
        open_claims = [c for c in claims if c["attempt"] not in runs]
        if not open_claims:
            self.gh.add_labels(self.ctl, issue["number"], [m.RUNNING])
            self.gh.remove_label(self.ctl, issue["number"], m.CLAIMED)
            return
        claim = open_claims[-1]
        if self.now() - ts(claim["created_at"]) < CLAIM_TIMEOUT:
            return
        not_before = (ts(claim["created_at"]) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        agent = self.cursor.find(claim["attempt"], not_before)
        if agent:
            return self.record(issue, claim["attempt"], agent)
        repo = self.repo_of(issue)
        self.gh.comment(self.ctl, issue["number"], "Attempt %d never reached Cursor; retrying once with a new attempt." % len(claims))
        self.release(issue, repo)
        self.maybe_start(issue)

    def follow(self, issue):
        cursor_id = list(self.runs(issue).values())[-1]
        try:
            run = self.cursor.latest_run(cursor_id)
        except cursor_api.CursorError as error:
            self.log("Could not read Cursor run %s for #%d: %s" % (cursor_id, issue["number"], error))
            return
        if run["status"] in cursor_api.ACTIVE:
            return
        prs = [b["prUrl"] for b in (run.get("git") or {}).get("branches", []) if b.get("prUrl")]
        if run["status"] == "FINISHED" and prs:
            self.gh.comment(self.ctl, issue["number"], "Cursor opened a fix: %s" % prs[0])
            self.gh.add_labels(self.ctl, issue["number"], [m.PR_OPEN])
        elif run["status"] == "FINISHED":
            self.gh.comment(self.ctl, issue["number"], "Cursor finished without a fix. Its triage:\n\n" + (run.get("result") or "(empty)")[:6000])
            self.gh.add_labels(self.ctl, issue["number"], [m.TRIAGED])
        else:
            self.gh.comment(self.ctl, issue["number"], "Attempt %d failed: the Cursor run ended as %s." % (len(self.claims(issue)), run["status"]))
        self.release(issue, self.repo_of(issue))

    def escalate(self, issue, reason):
        """D4: a triage comment, `agent:needs-human` and a GitHub assignment (GitHub emails the assignee). Never Slack."""
        who = self.config["escalate_to"]
        self.gh.comment(self.ctl, issue["number"], "This needs a person: %s. Assigned to @%s." % (reason, who))
        self.gh.add_labels(self.ctl, issue["number"], [m.NEEDS_HUMAN])
        self.gh.assign(self.ctl, issue["number"], [who])
        issue["labels"] = issue.get("labels", []) + [{"name": m.NEEDS_HUMAN}]
