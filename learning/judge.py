#!/usr/bin/env python3
"""Blind review of skill-text edits on learning PRs (D10), run by learn.yml.

  python3 -m learning.judge --control <agent-control checkout> --model <OpenAI model id>

For each open PR on a `learn/*` branch in a registered repo that edits skill text (`SKILL.md`, or Markdown under a
`skills/` folder), it shows the old and new text to an OpenAI model as "Version A" and "Version B" in an order
derived from the head commit, without saying which is new, and posts the verdict once per head commit. The
proposer runs on a Cursor model, so the judge is from another family; a PR whose proposer is an OpenAI model is
not judged. Env: GH_TOKEN (Contents and Pull requests read, Issues write), OPENAI_API_KEY.
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


def gh(method, path, body=None):
    command = ["gh", "api", "-X", method, path] + (["--input", "-"] if body is not None else [])
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("gh api %s %s failed: %s" % (method, path, result.stderr.strip()))
    return json.loads(result.stdout or "null")


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


def judge_pr(api, ask, model, repo, pr):
    number, head = pr["number"], pr["head"]["sha"]
    if any(MARKER in (c.get("body") or "") and head in c["body"] for c in api("GET", "/repos/%s/issues/%d/comments?per_page=100" % (repo, number))):
        return
    paths = [f["filename"] for f in api("GET", "/repos/%s/pulls/%d/files?per_page=100" % (repo, number))
             if f["status"] == "modified" and is_skill_text(f["filename"])]
    if not paths:
        return
    found = re.search(r"agent-kit:learning v1 ([^>]*?)\s*-->", pr.get("body") or "")
    fields = dict(t.split(":", 1) for t in (found.group(1).split() if found else []) if ":" in t)
    if SAME_FAMILY.search(fields.get("model", "")):
        verdict, text = "not-judged", "Blind review skipped: the proposer (%s) is from the judge's family; ask for a judge from another model family." % fields["model"]
    else:
        old = "\n\n".join("--- %s\n%s" % (p, text_at(api, repo, p, pr["base"]["sha"])) for p in paths)
        new = "\n\n".join("--- %s\n%s" % (p, text_at(api, repo, p, head)) for p in paths)
        new_first = int(hashlib.sha256(head.encode()).hexdigest(), 16) % 2 == 0
        answer = ask(PROMPT.format(cls=fields.get("class", "the mistake named in the PR"), a=new if new_first else old, b=old if new_first else new))
        match = re.search(r"\{.*\}", answer, re.S)
        reply = json.loads(match.group(0)) if match else {}
        better = reply.get("better")
        verdict = "tie" if better == "tie" else ("new" if (better == "A") == new_first else "old") if better in ("A", "B") else "unclear"
        text = {"new": "the new skill text is better", "old": "the old skill text is better", "tie": "no clear difference",
                "unclear": "the judge gave no usable answer"}[verdict]
        text = "Blind review by %s, a different model family from the proposer: %s. Reason: %s" % (model, text, reply.get("reason", "none given"))
    marker = "<!-- %s %s -->" % (MARKER, json.dumps({"head_sha": head, "judge": model, "verdict": verdict}, separators=(",", ":")))
    api("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body": marker + "\n" + text})


def run(repos, api, ask, model):
    for repo in repos:
        for pr in api("GET", "/repos/%s/pulls?state=open&per_page=100" % repo):
            if pr["head"]["ref"].startswith("learn/"):
                judge_pr(api, ask, model, repo, pr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--control", required=True)
    parser.add_argument("--model", required=True, help="OpenAI model id for the judge")
    args = parser.parse_args(argv)
    run(load_repos(args.control), gh, openai(args.model), args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
