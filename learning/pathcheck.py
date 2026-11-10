#!/usr/bin/env python3
"""INV-12 check for learning PRs: only added test or eval cases may change.

  pathcheck.py --files files.jsonl   # one GitHub "list pull request files" object per line, or a JSON array

Fails when the PR touches policy, the gate, verification, CI, graders or thresholds, deletes or renames a file,
edits an existing case, or changes anything that is not a test or eval case. Appending to a case list
(a JSONL line, or a JSON array entry whose previous line gains a comma) counts as adding a case.
"""
import argparse
import json
import sys
from fnmatch import fnmatch
from pathlib import Path

PROTECTED = [".agent-kit/*", ".github/*", ".cursor/*", "gate/*", "verify/*", "doctor/*", "schemas/*", "dispatcher/*",
             "learning/*", "*grade*.py", "*grader*", "*threshold*", "*pr_check.py", "CODEOWNERS", "AGENTS.md", "CLAUDE.md"]
CASES = ["tests/*", "test/*", "*/tests/*", "*/__tests__/*", "evals/*", "*/evals/*",
         "*.spec.ts", "*.test.ts", "*.spec.js", "*.test.js", "*test_*.py", "*_test.py", "*_test.go"]


def only_appends(patch):
    lines = (patch or "").splitlines()
    removed = [l[1:].rstrip() for l in lines if l.startswith("-") and not l.startswith("---")]
    added = {l[1:].rstrip() for l in lines if l.startswith("+") and not l.startswith("+++")}
    return bool(patch) and all(r + "," in added for r in removed)


def problems(files):
    out = []
    for f in files:
        path, status = f["filename"], f["status"]
        if any(fnmatch(path, p) for p in PROTECTED):
            out.append("%s: policy, gate, verification, CI, grader or threshold files need a human, not a learning PR" % path)
        elif not any(fnmatch(path, p) for p in CASES):
            out.append("%s: a learning PR may only add test or eval cases" % path)
        elif status == "added":
            continue
        elif status != "modified" or not only_appends(f.get("patch")):
            out.append("%s (%s): changes an existing case; only new cases are allowed" % (path, status))
    return out


def load(path):
    text = Path(path).read_text().strip()
    return json.loads(text) if text.startswith("[") else [json.loads(l) for l in text.splitlines() if l.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", required=True)
    found = problems(load(parser.parse_args(argv).files))
    for line in found:
        print("FAIL " + line)
    print("Learning path check: %s." % ("%d problem(s)" % len(found) if found else "only added cases, ok"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
