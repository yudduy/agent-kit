#!/usr/bin/env python3
"""Blind review of skill-text edits on learning PRs (D10), run by learn.yml.

  python3 -m learning.judge --control <agent-control checkout> --model <OpenAI model id>

For each open PR on a `learn/*` branch in a registered repo that edits skill text (`SKILL.md`, or Markdown under a
`skills/` folder), it shows the old and new text to an OpenAI model as "Version A" and "Version B" in an order
derived from the head commit, without saying which is new, and posts the verdict once per head commit. The
proposer runs on a Cursor model, so the judge is from another family; a PR whose learning marker names an OpenAI
model, or no model, is not judged. Env: GH_TOKEN (Contents and Pull requests read, Issues write), OPENAI_API_KEY.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

from learning.harvest import load_repos

MARKER = "agent-kit:judge v1"
SAME_FAMILY = re.compile(r"\b(gpt|codex|o\d)", re.I)
PROMPT = """Two versions of the same instruction file for coding agents follow. It should stop this mistake: {cls}.
Judge which version makes an agent less likely to repeat the mistake without adding noise or confusion.
Reply with JSON only: {{"better": "A" or "B" or "tie", "reason": "one sentence"}}.

===== Version A =====
{a}

===== Version B =====
{b}
"""


def _gh(args, stdin=None):
    result = subprocess.run(["gh", "api"] + args, input=stdin, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("gh api %s failed: %s" % (" ".join(args), result.stderr.strip()))
    return result.stdout


def gh(method, path, body=None):
    return json.loads(_gh(["-X", method, path] + (["--input", "-"] if body is not None else []),
                          json.dumps(body) if body is not None else None) or "null")


def gh_pages(path):
    """Every item of a list endpoint, across all pages."""
    return [json.loads(line) for line in _gh(["--paginate", path + ("&" if "?" in path else "?") + "per_page=100", "--jq", ".[]"]).splitlines() if line]


def openai(model):
    def ask(prompt):
        request = urllib.request.Request("https://api.openai.com/v1/responses", method="POST",
                                         data=json.dumps({"model": model, "input": prompt}).encode(),
                                         headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=300) as response:
            data = json.load(response)
        return "".join(c.get("text", "") for item in data.get("output", []) for c in item.get("content") or [] if c.get("type") == "output_text")
    return ask


def is_skill_text(path):
    return path.endswith("SKILL.md") or ("skills/" in path and path.endswith(".md"))


def text_at(api, repo, path, sha):
    return base64.b64decode(api("GET", "/repos/%s/contents/%s?ref=%s" % (repo, path, sha))["content"]).decode()


def judged(pages, me, repo, number, head):
    """True when this login already posted a verdict for this head commit; markers from anyone else do not count."""
    for c in pages("/repos/%s/issues/%d/comments" % (repo, number)):
        found = re.search(r"<!-- %s (\{.*?\}) -->" % MARKER, c.get("body") or "")
        if found and (c.get("user") or {}).get("login") == me and json_object(found.group(1)).get("head_sha") == head:
            return True
    return False


def json_object(answer):
    try:
        reply = json.loads(re.search(r"\{.*\}", answer, re.S).group(0))
    except (AttributeError, ValueError):
        return {}
    return reply if isinstance(reply, dict) else {}


def judge_pr(api, pages, ask, model, me, repo, pr):
    number, head = pr["number"], pr["head"]["sha"]
    if judged(pages, me, repo, number, head):
        return
    paths = [f["filename"] for f in pages("/repos/%s/pulls/%d/files" % (repo, number))
             if f["status"] == "modified" and is_skill_text(f["filename"])]
    if not paths:
        return
    found = re.search(r"agent-kit:learning v1 ([^>]*?)\s*-->", pr.get("body") or "")
    fields = dict(t.split(":", 1) for t in (found.group(1).split() if found else []) if ":" in t)
    if not fields.get("model"):
        verdict, text = "not-judged", "Blind review skipped: the PR does not name its proposer model in the learning marker, so a judge from another family cannot be chosen."
    elif SAME_FAMILY.search(fields["model"]):
        verdict, text = "not-judged", "Blind review skipped: the proposer (%s) is from the judge's family; ask for a judge from another model family." % fields["model"]
    else:
        old = "\n\n".join("--- %s\n%s" % (p, text_at(api, repo, p, pr["base"]["sha"])) for p in paths)
        new = "\n\n".join("--- %s\n%s" % (p, text_at(api, repo, p, head)) for p in paths)
        new_first = int(hashlib.sha256(head.encode()).hexdigest(), 16) % 2 == 0
        answer = ask(PROMPT.format(cls=fields.get("class", "the mistake named in the PR"), a=new if new_first else old, b=old if new_first else new))
        reply = json_object(answer)
        better = reply.get("better")
        verdict = "tie" if better == "tie" else ("new" if (better == "A") == new_first else "old") if better in ("A", "B") else "unclear"
        text = {"new": "the new skill text is better", "old": "the old skill text is better", "tie": "no clear difference",
                "unclear": "the judge gave no usable answer"}[verdict]
        text = "Blind review by %s, a different model family from the proposer: %s. Reason: %s" % (model, text, reply.get("reason", "none given"))
    marker = "<!-- %s %s -->" % (MARKER, json.dumps({"head_sha": head, "judge": model, "verdict": verdict}, separators=(",", ":")))
    api("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body": marker + "\n" + text})


def run(repos, api, pages, ask, model):
    me = api("GET", "/user")["login"]
    for repo in repos:
        for pr in pages("/repos/%s/pulls?state=open" % repo):
            if pr["head"]["ref"].startswith("learn/"):
                judge_pr(api, pages, ask, model, me, repo, pr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--control", required=True)
    parser.add_argument("--model", required=True, help="OpenAI model id for the judge")
    args = parser.parse_args(argv)
    run(load_repos(args.control), gh, gh_pages, openai(args.model), args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
