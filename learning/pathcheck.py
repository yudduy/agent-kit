#!/usr/bin/env python3
"""INV-12 check for learning PRs (head branch `learn/*`), run by each repo's agent-learning-check workflow.

  pathcheck.py --files files.jsonl   # one GitHub "list pull request files" object per line, or a JSON array

Exit 0: only added test or eval cases (new files, or lines appended to a `.jsonl` case list).
Exit 10: also a higher-level fix (a new check, a verify-skill or skill-text edit, other code); allowed, and the
         workflow labels the PR `governance` so only a human can approve it.
Exit 1: the PR deletes or renames a file, edits an existing test or eval case (one added `return` can switch a test
        off), or touches policy, journeys, seed, graders, thresholds, the gate, verify, or an existing CI workflow.
"""
import argparse
import json
import sys
from fnmatch import fnmatch
from pathlib import Path

NEEDS_HUMAN = [".agent-kit/*", "gate/*", "verify/*", "learning/*", "schemas/*", "*grade*.py", "*grader*", "*threshold*"]
CASES = ["tests/*", "test/*", "*/tests/*", "*/__tests__/*", "evals/*", "*/evals/*", "*fixtures/*",
         "*.spec.ts", "*.test.ts", "*.spec.js", "*.test.js", "*test_*.py", "*_test.py", "*_test.go"]
GOVERNANCE = 10


def only_additions(patch):
    return bool(patch) and not any(l.startswith("-") and not l.startswith("---") for l in patch.splitlines())


def check(files):
    """(problems, governance paths) for a PR's file list."""
    problems, governance = [], []
    for f in files:
        path, status = f["filename"], f["status"]
        is_case = any(fnmatch(path, p) for p in CASES)
        if status not in ("added", "modified"):
            problems.append("%s (%s): a learning PR may not delete or rename files" % (path, status))
        elif any(fnmatch(path, p) for p in NEEDS_HUMAN):
            problems.append("%s: policy, journeys, graders, thresholds, the gate and verify change only by a human" % path)
        elif status == "modified" and fnmatch(path, ".github/workflows/*"):
            problems.append("%s: existing CI workflows change only by a human; add a new workflow instead" % path)
        elif is_case and status == "modified" and not (path.endswith(".jsonl") and only_additions(f.get("patch"))):
            problems.append("%s: edits an existing case; only new cases are allowed" % path)
        elif not is_case:
            governance.append(path)
    return problems, governance


def load(path):
    text = Path(path).read_text().strip()
    return json.loads(text) if text.startswith("[") else [json.loads(l) for l in text.splitlines() if l.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", required=True)
    problems, governance = check(load(parser.parse_args(argv).files))
    for line in problems:
        print("FAIL " + line)
    for path in governance:
        print("GOVERNANCE %s: more than an added case, so only a human can approve this PR" % path)
    if problems:
        print("Learning path check: %d problem(s)." % len(problems))
        return 1
    print("Learning path check: %s." % ("passes, labelled governance" if governance else "only added cases"))
    return GOVERNANCE if governance else 0


if __name__ == "__main__":
    sys.exit(main())
