"""One dispatcher tick (D4): intake errors, then start, reconcile or follow each incident's Cursor run.

Run state lives only in agent-control issues (C3), as markers the dispatcher writes, plus Cursor's run state.
Labels are display only and repaired each tick. The workflow's global concurrency group makes this the single
writer, and the dispatcher is the only caller of the Cursor API.
"""
import re
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
IDLE_CLOSE = timedelta(days=30)
ACTIVE_WINDOW = timedelta(days=7)  # a closed issue updated within this is read in case its run is still going


def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def labels_of(issue):
    return {label["name"] for label in issue.get("labels", [])}


# Comment-derived run state. Dispatcher markers count only from the dispatcher's own login, and only after its
# latest reopen marker (the current claim generation).
def mine(item):
    return (item.get("user") or {}).get("login") == DISPATCHER_LOGIN


def own(kind, comments):
    """Fields of every `kind` marker that opens a dispatcher comment."""
    return [{**f, "created_at": c["created_at"], "comment_id": c.get("id")} for c in comments
            if mine(c) and isinstance(f := m.lead(kind, c["body"]), dict)]


def current(comments):
    reopens = [i for i, c in enumerate(comments) if mine(c) and m.lead("reopen", c["body"])]
    return comments[reopens[-1] + 1:] if reopens else comments


def claims_in(comments):
    return own("claim", comments)


def runs_in(comments):
    return {r["attempt"]: r["cursor_id"] for r in own("run", comments)}


AGENT_NAME = re.compile(r"^agent-kit ([0-9a-f]{16}) (\S+)$")


def state(comments):
    """What the markers of the current generation say: phase idle, claimed or running, and how it ended."""
    claims, runs = claims_in(comments), runs_in(comments)
    results = {r["attempt"]: r["outcome"] for r in own("result", comments)}
    claim = claims[-1] if claims else None
    open_claim = claim and claim["attempt"] not in results
    return {"phase": ("running" if claim["attempt"] in runs else "claimed") if open_claim else "idle",
            "claim": claim, "cursor_id": runs.get(claim["attempt"]) if claim else None, "attempts": len(claims),
            "outcome": results.get(claim["attempt"]) if claim else None, "escalated": bool(own("escalate", comments))}


class Tick:
    def __init__(self, config, gh, cursor, pull, now, new_id, log=print, reader=None):
        """`config` comes from `dispatcher.__main__.load`. `gh` writes agent-control issues; `reader` (default `gh`)
        reads policy files from onboarded repos."""
        self.config, self.gh, self.cursor, self.pull, self.log = config, gh, cursor, pull, log
        self.reader = reader or gh
        self.now, self.new_id = now, new_id
        self.ctl, self.kit = config["control_repo"], KIT_CAPS
        self.cost = config["run_cost_usd"]

    def run(self):
        self.started = self.now()  # events after this may be missing from this tick's pull
        self._comments = {}
        self.issues = self.gh.issues(self.ctl, state="all")  # not by label: labels are display only
        self.intake()
        running = self.cursor_running()
        today = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.spent = {}
        tracked = [i for i in self.issues if self.repo_of(i) in self.config["repos"]
                   and (i["state"] == "open" or self.now() - ts(i["updated_at"]) < ACTIVE_WINDOW or self.fp_of(i) in running)]
        busy = {(self.repo_of(i), self.fp_of(i)) for i in tracked if self.state(i)["phase"] != "idle"}
        busy |= {(self.repo_of(self.by_fp[fp]), fp) for fp in running if fp in self.by_fp}  # a run Cursor shows still counts
        self.active = {}
        for repo, _ in busy:
            self.active[repo] = self.active.get(repo, 0) + 1
        self.cancel_unreserved(running)
        for issue in tracked:
            repo = self.repo_of(issue)
            if ts(issue["updated_at"]) >= today:
                claims_today = [c for c in claims_in(self.comments(issue, since_reopen=False)) if ts(c["created_at"]) >= today]
                self.spent[repo] = self.spent.get(repo, 0) + self.cost * len(claims_today)
        self.stale = self.heartbeat_stale()
        self.policies = {}
        for issue in sorted(tracked, key=lambda i: i["number"]):
            self.step(issue)
            self.repair_labels(issue)

    def idle(self, issue):
        """No update for 30 days, and its error was not in this tick's pull from a source that answered."""
        fp = (m.parse("intake", issue.get("body")) or {}).get("fp")
        return self.now() - ts(issue["updated_at"]) > IDLE_CLOSE and self.repo_of(issue) in self.pulled and fp not in self.seen

    def close_idle(self, issue):
        self.say(issue, m.json_marker("idle", {"pulled_at": self.started.isoformat()})
                 + "\nClosing: no new event and no active run for 30 days. It reopens if the error comes back.")
        self.gh.update_issue(self.ctl, issue["number"], state="closed")
        issue["state"] = "closed"

    # Intake (C3): one issue per fingerprint, reopened when a closed error comes back.
    def intake(self):
        self.by_fp = by_fp = {}
        self.seen, self.pulled = set(), set()
        # an intake marker counts only when it opens an issue the dispatcher opened
        self.issues = [i for i in self.issues if "pull_request" not in i and mine(i) and m.lead("intake", i.get("body"))]
        for issue in self.issues:
            by_fp[self.fp_of(issue)] = issue
        for repo in self.config["repos"]:
            try:
                rows = self.pull(repo, self.config["sources"].get(repo, {}))
            except Exception as error:  # one broken source must not stop the tick
                self.log("Could not read errors for %s: %s" % (repo, error))
                continue
            if getattr(rows, "complete", True):  # only a full read can show that an error stopped
                self.pulled.add(repo)
            for row in rows:
                fp = m.fingerprint(row["source"], row["src_id"])
                self.seen.add(fp)
                issue = by_fp.get(fp)
                if issue is None:
                    by_fp[fp] = issue = self.open_incident(repo, fp, row)
                    self.issues.append(issue)
                elif issue["state"] == "closed" and not self.ignored(issue) and row["last_seen"] \
                        and ts(row["last_seen"]) > self.closed_cutoff(issue):
                    self.reopen(issue, row)

    def closed_cutoff(self, issue):
        """Events after this reopen a closed issue: its close time, or for an idle close the start of that tick's pull."""
        closes = [e for e in self.gh.events(self.ctl, issue["number"]) if e.get("event") == "closed"]
        if not closes or not mine({"user": closes[-1]["actor"]}):  # the latest close was not the dispatcher's idle close
            return ts(issue["closed_at"])
        idle = own("idle", self.comments(issue, since_reopen=False))
        return ts(idle[-1]["pulled_at"]) if idle else ts(issue["closed_at"])

    def open_incident(self, repo, fp, row):
        body = "\n".join([m.intake(fp, row["source"], row["src_id"], row["first_seen"], repo),
                          "**Error:** %s" % row["title"], "**Where:** %s" % (row["where"] or "unknown"),
                          "**App:** %s" % repo, "**Seen:** %s times, first at %s" % (row["count"], row["first_seen"]),
                          "**Source:** %s" % row["link"]])
        title = "%s error in %s: %s" % (row["source"].capitalize(), repo.split("/")[-1], row["title"])
        issue = self.gh.create_issue(self.ctl, title[:250], body, [m.INCIDENT, m.NEEDS_DECISION])
        self._comments[issue["number"]] = []
        return issue

    def reopen(self, issue, row):
        """Reopen with a fresh decision and attempt count, unless a run is still active: then keep following it (INV-8)."""
        self.gh.update_issue(self.ctl, issue["number"], state="open")
        issue["state"] = "open"
        if self.state(issue)["phase"] != "idle":
            self.say(issue, "This error came back (last seen %s) while its run is still active." % row["last_seen"])
            return
        self.gh.remove_label(self.ctl, issue["number"], m.DECIDED)
        self.gh.add_labels(self.ctl, issue["number"], [m.NEEDS_DECISION])
        self.say(issue, m.json_marker("reopen", {"last_seen": row["last_seen"]})
                 + "\nThis error came back after the issue was closed (last seen %s)." % row["last_seen"])

    # Comments: one read per issue per tick; every write is appended so later steps see it.
    def comments(self, issue, since_reopen=True):
        if issue["number"] not in self._comments:
            self._comments[issue["number"]] = self.gh.comments(self.ctl, issue["number"])
        everything = self._comments[issue["number"]]
        return current(everything) if since_reopen else everything

    def say(self, issue, body):
        self.comments(issue)
        posted = self.gh.comment(self.ctl, issue["number"], body)
        self._comments[issue["number"]].append(posted)
        return posted

    def state(self, issue):
        return state(self.comments(issue))

    def ignored(self, issue):
        decisions = [d for d in (m.decision(c, self.config["deciders"]) for c in self.comments(issue)) if d]
        return bool(decisions) and decisions[-1]["action"] == "ignore"

    def repo_of(self, issue):
        return (m.lead("intake", issue.get("body")) or {}).get("repo")

    def fp_of(self, issue):
        return (m.lead("intake", issue.get("body")) or {}).get("fp")

    def cursor_running(self):
        """{fp: [(agent, attempt)]} for this kit's agents that Cursor shows with a turn running."""
        found = {}
        for agent in self.cursor.active():
            named = AGENT_NAME.match(agent.get("name") or "")
            if named:
                found.setdefault(named.group(1), []).append((agent, named.group(2)))
        return found

    def cancel_unreserved(self, running):
        """A running agent whose attempt is not this incident's open claim (a late queued follow-up, or an old
        attempt) has no reservation, so it is cancelled at once: one active run per fingerprint (INV-8)."""
        for fp, agents in running.items():
            issue = self.by_fp.get(fp)
            if issue is None:
                continue
            st = self.state(issue)
            for agent, attempt in agents:
                if st["phase"] != "idle" and st["claim"]["attempt"] == attempt:
                    continue
                try:
                    run = self.cursor.latest_run(agent["id"])
                    self.cursor.cancel(agent["id"], run["id"])
                except cursor_api.CursorError as error:
                    self.log("Could not cancel unreserved agent %s: %s" % (agent["id"], error))
                    continue
                self.say(issue, "Cancelled a Cursor run this incident had no open claim for: %s." % agent.get("url", agent["id"]))

    def repair_labels(self, issue):
        """Labels show the markers' state; the dispatcher's own labels are reset to match each tick."""
        st = self.state(issue)
        want = {m.INCIDENT: True, m.CLAIMED: st["phase"] == "claimed", m.RUNNING: st["phase"] == "running",
                m.PR_OPEN: st["phase"] == "idle" and st["outcome"] == "pr",
                m.TRIAGED: st["phase"] == "idle" and st["outcome"] == "triage", m.NEEDS_HUMAN: st["escalated"]}
        have = labels_of(issue)
        add = [label for label, on in want.items() if on and label not in have]
        if add:
            self.gh.add_labels(self.ctl, issue["number"], add)
        for label in (label for label, on in want.items() if not on and label in have):
            self.gh.remove_label(self.ctl, issue["number"], label)
        issue["labels"] = [{"name": n} for n in (have - {l for l, on in want.items() if not on}) | set(add)]

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

    # Per-issue state machine, driven by markers only.
    def step(self, issue):
        st = self.state(issue)
        if st["phase"] == "running":
            return self.follow(issue, st)
        if st["phase"] == "claimed":
            return self.reconcile(issue, st)
        self.refuse_steers(issue, "no active run")
        if issue["state"] != "open":
            return
        if self.idle(issue):
            return self.close_idle(issue)
        self.maybe_start(issue, st)

    def maybe_start(self, issue, st):
        if st["outcome"] in ("pr", "triage") or st["escalated"] or self.ignored(issue):
            return
        repo = self.repo_of(issue)
        loaded = self.caps(repo)
        if loaded is None:
            self.log("Skipping %s: no valid .agent-kit/policy.yml on its default branch." % repo)
            return
        caps, approvers = loaded
        if st["attempts"] >= caps["max_attempts"]:
            return self.escalate(issue, "%d attempts did not produce a fix or a triage" % caps["max_attempts"])
        decisions = [d for d in (m.decision(c, self.config["deciders"]) for c in self.comments(issue)) if d]
        if decisions:
            prompt, model = decisions[-1]["prompt"], decisions[-1].get("model")
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
        self.start(issue, repo, prompt, model, st["attempts"] + 1)

    def paid_ok(self, issue, approvers):
        """`agent:paid-ok` is on the issue now, and an approver (a user, not a bot) applied it last."""
        if m.PAID_OK not in labels_of(issue):
            return False
        applied = [e for e in self.gh.events(self.ctl, issue["number"]) if e.get("event") == "labeled" and e["label"]["name"] == m.PAID_OK]
        return bool(applied) and applied[-1]["actor"]["login"] in approvers and applied[-1]["actor"].get("type") == "User"

    def start(self, issue, repo, prompt, model, number):
        attempt = self.new_id()
        self.say(issue, m.json_marker("claim", {"attempt": attempt, "at": self.now().isoformat()})
                 + "\nAttempt %d claimed; starting a Cursor run." % number)
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
            self.finish(issue, attempt, "refused", "Attempt %d failed: Cursor refused the run (%s)." % (number, error.status))
            return
        self.record(issue, attempt, agent)

    def record(self, issue, attempt, agent):
        self.say(issue, m.json_marker("run", {"attempt": attempt, "cursor_id": agent["id"]})
                 + "\nCursor run started: %s" % agent.get("url", agent["id"]))

    def finish(self, issue, attempt, outcome, text):
        """Close the claim with an outcome marker; this, not a label, frees its reservation."""
        self.say(issue, m.json_marker("result", {"attempt": attempt, "outcome": outcome}) + "\n" + text)
        repo = self.repo_of(issue)
        self.active[repo] = self.active.get(repo, 1) - 1

    def reconcile(self, issue, st):
        """INV-8: a claim with no run line is looked up in Cursor before any retry."""
        claim = st["claim"]
        if self.now() - ts(claim["created_at"]) < CLAIM_TIMEOUT:
            return
        not_before = (ts(claim["created_at"]) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        agent = self.cursor.find(claim["attempt"], not_before)
        if agent:
            return self.record(issue, claim["attempt"], agent)
        self.refuse_steers(issue, "its claim never reached Cursor")
        if issue["state"] != "open":
            return self.finish(issue, claim["attempt"], "lost", "Attempt %d never reached Cursor; the issue is closed, so no retry." % st["attempts"])
        self.finish(issue, claim["attempt"], "lost", "Attempt %d never reached Cursor; retrying once with a new attempt." % st["attempts"])
        self.maybe_start(issue, self.state(issue))

    def follow(self, issue, st):
        try:
            run = self.cursor.latest_run(st["cursor_id"])
        except cursor_api.CursorError as error:
            self.log("Could not read Cursor run %s for #%d: %s" % (st["cursor_id"], issue["number"], error))
            return
        if run["status"] in cursor_api.ACTIVE:
            return self.send_steers(issue, st, run)
        if self.steer_may_start_a_run(issue, st, run):
            return  # the claim stays reserved: a steer sent to this run may still start the next one
        attempt = st["claim"]["attempt"]
        prs = [b["prUrl"] for b in (run.get("git") or {}).get("branches", []) if b.get("prUrl")]
        if run["status"] == "FINISHED" and prs:
            self.finish(issue, attempt, "pr", "Cursor opened a fix: %s" % m.inert(prs[0]))
        elif run["status"] == "FINISHED":
            self.finish(issue, attempt, "triage", "Cursor finished without a fix. Its triage:\n\n" + m.inert(run.get("result") or "(empty)")[:6000])
        else:
            self.finish(issue, attempt, "failed", "Attempt %d failed: the Cursor run ended as %s." % (st["attempts"], run["status"]))

    # Steering (D5): deciders post requests; only the dispatcher calls Cursor.
    def steers(self, issue):
        """Pending requests, once each by id."""
        seen = {s["id"] for s in own("steered", self.comments(issue))}
        pending = []
        for request in (m.steer_request(c, self.config["deciders"]) for c in self.comments(issue)):
            if request and request["id"] not in seen:
                seen.add(request["id"])
                pending.append(request)
        return pending

    def sent_steers(self, issue, st):
        """Steers sent into this claim's agent, newest last."""
        return [s for s in own("steered", self.comments(issue)) if s.get("attempt") == st["claim"]["attempt"] and s.get("after_run")
                and s.get("delivery") != "refused"]

    def steer_may_start_a_run(self, issue, st, run):
        """A steer sent into this run can still become the agent's next run (a queued steer, or one the turn ended
        before reading), so the claim stays reserved for 10 minutes after the run ends or a later run appears."""
        sent = [s for s in self.sent_steers(issue, st) if s["after_run"] == run["id"]]
        return bool(sent) and self.now() - ts(run.get("updatedAt") or self.now().isoformat()) < CLAIM_TIMEOUT

    def send_steers(self, issue, st, run):
        for request in self.steers(issue):
            if any(s["delivery"] != "steered" and s["after_run"] == run["id"] for s in self.sent_steers(issue, st)):
                return  # a queued prompt has not started yet; send the next steer after it does
            # The intent is recorded before the call, so a failed write after it can never cause a second send.
            fields = {"id": request["id"], "delivery": "sending", "attempt": st["claim"]["attempt"], "after_run": run["id"]}
            intent = self.say(issue, m.json_marker("steered", fields) + "\nSending this steer to the running agent.")
            try:
                fields["delivery"], note = self.cursor.steer(st["cursor_id"], request["prompt"]).get("delivery", "unknown"), "sent"
            except cursor_api.Ambiguous:
                fields["delivery"], note = "unknown", "may have arrived; it is not retried"
            except cursor_api.CursorError as error:
                fields["delivery"], note = "refused", "refused by Cursor (%s)" % error.status
            body = m.json_marker("steered", fields) + "\nSteer %s (%s)." % (note, fields["delivery"])
            try:
                self.gh.edit_comment(self.ctl, intent["id"], body)
                intent["body"] = body
            except Exception as error:  # the intent marker already blocks a resend
                self.log("Could not record the steer result on #%d: %s" % (issue["number"], error))

    def refuse_steers(self, issue, reason):
        for request in self.steers(issue):
            self.say(issue, m.json_marker("steered", {"id": request["id"], "delivery": "refused"})
                     + "\nSteer not sent: %s. Only the current claim's active run can be steered." % reason)

    def escalate(self, issue, reason):
        """D4: a triage comment, `agent:needs-human` and a GitHub assignment (GitHub emails the assignee). Never Slack."""
        who = self.config["escalate_to"]
        self.say(issue, m.json_marker("escalate", {"reason": reason}) + "\nThis needs a person: %s. Assigned to @%s." % (reason, who))
        self.gh.assign(self.ctl, issue["number"], [who])
