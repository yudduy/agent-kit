#!/usr/bin/env python3
"""Weekly learning harvest (D10): turn outside signals on agent PRs into C6 case lines. Read-only GitHub queries.

Run from a kit checkout: `python3 -m learning.harvest --config ... --cases ...`.

  harvest.py --config agent-kit.json --cases log/learning/cases.jsonl [--days 21] [--propose]

Signals per onboarded repo: human edits to agent PR bodies, human review comments on merged agent PRs, reverts and
fix-ups within 14 days, `agent-gate` failures, and false greens (gate passed, then a revert or the same `fp` reopened
within 14 days). Bots and the PR author never count. New cases are appended to --cases; a class qualifies at 2 cases.
--propose starts one Cursor run per class that qualified this week, with learning/propose.md as its prompt.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli.onboard import PSTACK
from dispatcher import cursor

PR_FIELDS = """... on PullRequest {
  number title url body mergedAt headRefName author { login __typename }
  userContentEdits(first: 50) { nodes { editedAt editor { login __typename } diff } }
  comments(first: 50) { nodes { author { login __typename } body url createdAt } }
  reviews(first: 30) { nodes { author { login __typename } body url createdAt
    comments(first: 30) { nodes { author { login __typename } body url createdAt } } } }
  commits(last: 20) { nodes { commit { oid checkSuites(first: 10) { nodes {
    checkRuns(first: 5, filterBy: {checkName: "agent-gate"}) { nodes { conclusion summary url completedAt } } } } } } }
}"""
ISSUE_FIELDS = """... on Issue { number url body
  timelineItems(first: 20, itemTypes: [REOPENED_EVENT]) { nodes { ... on ReopenedEvent { createdAt } } } }"""
SEARCH = """query($q: String!, $n: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $n, after: $after) { pageInfo { hasNextPage endCursor } nodes { %s } } }"""
TOPICS = [
    ("pr-description", r"\b(title|description|summary|pr body|wording|jargon|unclear|confusing|plain(er)? language|screenshot|before and after)\b"),
    ("data-not-persisted", r"\b(persist\w*|saved?|stored|database|db|row|migration)\b"),
    ("missing-test", r"\b(tests?|specs?|regression|asserts?|coverage)\b"),
    ("scope", r"\b(unrelated|scope|too (big|large)|split|revert this part)\b"),
]
FP = re.compile(r"\bfp:([0-9a-f]{16})\b")
WINDOW = timedelta(days=14)


def gh_graphql(query, **variables):
    assert query.lstrip().startswith("query"), "harvest runs read-only queries only"
    command = ["gh", "api", "graphql", "-f", "query=" + query]
    for key, value in variables.items():
        if value is not None:
            command += ["-F" if isinstance(value, int) else "-f", "%s=%s" % (key, value)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        sys.exit("GitHub query failed: " + (result.stderr.strip().splitlines() or ["unknown error"])[-1])
    return json.loads(result.stdout)["data"]


def search(gql, fields, q):
    after, nodes = None, []
    while True:
        page = gql(SEARCH % fields, q=q, n=50, after=after)["search"]
        nodes += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def short(text, size=280):
    text = " ".join(re.sub(r"(?s)<!--.*?-->", "", text or "").split())
    return text if len(text) <= size else text[:size - 1] + "…"


class Harvest:
    def __init__(self, config, gql, now):
        self.config, self.gql, self.now = config, gql, now
        self.agents = set(config.get("agent_logins", []))

    def is_bot(self, actor):
        actor = actor or {}
        login = actor.get("login") or ""
        return actor.get("__typename") == "Bot" or login.endswith("[bot]") or login in self.agents

    def is_agent_pr(self, pr):
        return self.is_bot(pr.get("author")) or (pr.get("headRefName") or "").startswith("cursor/")

    def human(self, actor, pr):
        return actor and not self.is_bot(actor) and actor.get("login") != (pr.get("author") or {}).get("login")

    def case(self, signal, repo, pr, at, evidence, excerpt, proposed_class, source="github"):
        digest = hashlib.sha256((signal + evidence).encode()).hexdigest()[:8]
        return {"v": 1, "id": "case-%s-%s" % (at[:10], digest), "at": at, "source": source, "signal": signal,
                "repo": repo, "pr": pr["number"], "evidence": evidence, "excerpt": short(excerpt), "proposed_class": proposed_class}

    def run(self, days):
        since = (self.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        reopened = self.reopened_fps(since)
        cases = []
        for repo in self.config["repos"]:
            merged = search(self.gql, PR_FIELDS, "repo:%s is:pr is:merged merged:>=%s" % (repo, since))
            for pr in (p for p in merged if self.is_agent_pr(p)):
                cases += self.from_pr(repo, pr, merged, reopened)
        return cases

    def reopened_fps(self, since):
        q = 'repo:%s is:issue label:"agent:incident" updated:>=%s' % (self.config["control_repo"], since)
        out = {}
        for issue in search(self.gql, ISSUE_FIELDS, q):
            fp = re.search(r"agent-kit:intake v1 fp:([0-9a-f]{16})", issue.get("body") or "")
            if fp:
                out[fp.group(1)] = [(e["createdAt"], issue["url"]) for e in issue["timelineItems"]["nodes"] if e]
        return out

    def from_pr(self, repo, pr, merged, reopened):
        cases = []
        edits = sorted((e for e in pr["userContentEdits"]["nodes"] if e and e.get("diff") is not None), key=lambda e: e["editedAt"])
        for before, after in zip(edits, edits[1:]):
            if self.human(after.get("editor"), pr):
                diff = [l for l in difflib.unified_diff(before["diff"].splitlines(), after["diff"].splitlines(), lineterm="", n=0)
                        if l[:1] in "+-" and l[:3] not in ("+++", "---")]
                cases.append(self.case("duy_edit", repo, pr, after["editedAt"], pr["url"] + "#edit-" + after["editedAt"],
                                       "%s edited the PR body: %s" % (after["editor"]["login"], " / ".join(diff[:4])), "pr-description"))
        comments = [c for c in pr["comments"]["nodes"] if c]
        for review in (r for r in pr["reviews"]["nodes"] if r):
            comments += [review] + [c for c in review["comments"]["nodes"] if c]
        for c in comments:
            if self.human(c.get("author"), pr) and topic(c.get("body")):
                cases.append(self.case("review_comment", repo, pr, c["createdAt"], c["url"], c["body"], topic(c["body"])))
        gate = [r for n in pr["commits"]["nodes"] for s in n["commit"]["checkSuites"]["nodes"] for r in s["checkRuns"]["nodes"] if r]
        for r in (r for r in gate if r["conclusion"] == "FAILURE"):
            cases.append(self.case("gate_failure", repo, pr, r["completedAt"], r["url"], r.get("summary") or "", gate_class(r.get("summary")), "gate"))
        followups = self.followups(pr, merged)
        for later, kind in followups:
            cases.append(self.case("revert_14d", repo, pr, later["mergedAt"], later["url"], "%s: %s" % (kind, later["title"]), "reverted-fix"))
        if any(r["conclusion"] == "SUCCESS" for r in gate):
            merged_at = ts(pr["mergedAt"])
            fps = set(FP.findall(pr.get("body") or ""))
            returns = [(at, url) for fp in fps for at, url in reopened.get(fp, []) if merged_at < ts(at) <= merged_at + WINDOW]
            returns += [(l["mergedAt"], l["url"]) for l, kind in followups if kind == "revert"]
            for at, url in returns[:1]:
                cases.append(self.case("false_green", repo, pr, at, url, "The gate passed %s, then it was reverted or its error came back." % pr["url"], "false-green", "gate"))
        return cases

    def followups(self, pr, merged):
        """Later PRs merged within 14 days that revert or fix up this one."""
        ref = re.compile(r"(?<![\w/])#%d\b" % pr["number"])
        out = []
        for later in merged:
            if later["number"] == pr["number"] or not (ts(pr["mergedAt"]) < ts(later["mergedAt"]) <= ts(pr["mergedAt"]) + WINDOW):
                continue
            text = (later["title"] or "") + "\n" + (later.get("body") or "")
            if later["title"].startswith('Revert "%s"' % pr["title"]) or re.search(r"\bReverts [\w.-]+/[\w.-]+#%d\b" % pr["number"], text):
                out.append((later, "revert"))
            elif ref.search(text) and re.search(r"\b(fix|fixes|fixup|fix-up|follow-?up)\b", text, re.I):
                out.append((later, "fix-up"))
        return out


def topic(text):
    """The first topic a comment raises, or None for comments like "LGTM" that teach nothing."""
    return next((slug for slug, pattern in TOPICS if re.search(pattern, short(text), re.I)), None)


def gate_class(summary):
    match = re.search(r"```json\s*(\{.*?\})\s*```", summary or "", re.S)
    try:
        reasons = json.loads(match.group(1)).get("reasons") if match else None
    except ValueError:
        reasons = None
    if not reasons:
        return "gate-failure"
    return "gate-" + "-".join(re.findall(r"[a-z]+", re.sub(r"\d+|`[^`]*`", "", reasons[0].lower())))[:60]


def classes(cases):
    return Counter((c["repo"], c["proposed_class"]) for c in cases)


def newly_qualified(old, new, threshold=2):
    before, after = classes(old), classes(old + new)
    return sorted(k for k, n in after.items() if n >= threshold and before.get(k, 0) < threshold)


def propose(client, config, repo, proposed_class, cases):
    lines = "\n".join(json.dumps(c) for c in cases if c["repo"] == repo and c["proposed_class"] == proposed_class)
    prompt = (Path(__file__).with_name("propose.md").read_text()
              .replace("{{CLASS}}", proposed_class).replace("{{REPO}}", repo).replace("{{CASES}}", lines)
              .replace("{{PSTACK_URL}}", PSTACK[0]).replace("{{PSTACK_SHA}}", PSTACK[1]))
    attempt = str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-kit-learning:%s:%s:%s" % (repo, proposed_class, lines)))
    branch = config["repos"][repo].get("branch", "main")
    return client.create(attempt, "agent-kit learning %s %s" % (repo, proposed_class), prompt, repo, branch)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--cases", required=True, help="learning/cases.jsonl on the decisions-log branch")
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--propose", action="store_true")
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    path = Path(args.cases)
    old = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
    seen = {c["id"] for c in old}
    new = []
    for c in Harvest(config, gh_graphql, lambda: datetime.now(timezone.utc)).run(args.days):
        if c["id"] not in seen:
            seen.add(c["id"])
            new.append(c)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.writelines(json.dumps(c) + "\n" for c in new)
    qualified = newly_qualified(old, new)
    print("Harvest: %d new cases; %d classes qualified this run." % (len(new), len(qualified)))
    for repo, name in qualified:
        print("- %s %s" % (repo, name))
        if args.propose:
            agent = propose(cursor.Client(os.environ["CURSOR_API_KEY"]), config, repo, name, old + new)
            print("  learning run: %s" % agent.get("url", agent["id"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
