#!/usr/bin/env python3
"""INV-13 proof for learning PRs (head branch `learn/*`), run by each repo's `learning-proof` check.

  PYTHONPATH=kit python3 -m learning.proof --files files.jsonl --base base --head head --case-cmd 'npx playwright test {case}' --suite-cmd 'npm test'
      [--setup 'npm ci'] [--summary "$GITHUB_STEP_SUMMARY"]

A case is each test or eval file the PR adds, or a `.jsonl` case list it appends to (pathcheck's patterns). Each case
is copied into the base tree and run there (it must fail), then run in the head tree (it must pass); then the full
suite runs on head (it must pass). A case whose added lines hold no assertion is a failed proof, so an empty spec
proves nothing. Exit 0 only if there is at least one case and every step holds.
"""
import argparse
import re
import shlex
import shutil
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

from learning.pathcheck import CASES, HOOKS, load

ASSERTION = re.compile(r"\b(assert\w*|expect\w*|should)\b|\.to[A-Z]\w*\(")
COMMENT = re.compile(r"^\s*(#|//|/?\*)")


def cases(files):
    out = []
    for f in files:
        path = f["filename"]
        if any(fnmatch(path, p) for p in HOOKS) or not any(fnmatch(path, p) for p in CASES):
            continue
        if f["status"] == "added" or (f["status"] == "modified" and path.endswith(".jsonl")):
            out.append((path, [l[1:] for l in (f.get("patch") or "").splitlines() if l.startswith("+") and not l.startswith("+++")]))
    return out


def asserts(lines):
    return any(ASSERTION.search(l) for l in lines if not COMMENT.match(l))


def sh(command, cwd, timeout):
    print("$ (%s) %s" % (cwd.name, command))
    try:
        return subprocess.run(command, shell=True, cwd=cwd, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print("timed out after %ds" % timeout)
        return None


def prove(files, base, head, case_cmd, suite_cmd, setup=None, timeout=900):
    """(ok, counts) for a PR's file list, base and head checkouts and the repo's test commands."""
    base, head, found = Path(base), Path(head), cases(files)
    counts = {"cases": len(found), "failed_on_base": 0, "passed_on_head": 0, "no_assertion": 0, "suite": "not run"}
    if not case_cmd or not suite_cmd or "{case}" not in case_cmd:
        print("FAIL the repo variables AGENT_KIT_CASE_CMD (with {case}) and AGENT_KIT_SUITE_CMD must be set")
        return False, counts
    if not found:
        print("FAIL the PR adds no test or eval case, so nothing proves the change")
        return False, counts
    runnable = []
    for path, added in found:
        if asserts(added):
            runnable.append(path)
            (base / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(head / path, base / path)
        else:
            counts["no_assertion"] += 1
            print("FAIL %s: its added lines hold no assertion" % path)
    # Every base run ends before head setup or head runs start, so code only on head cannot change the base results.
    for tree, key, want in ((base, "failed_on_base", False), (head, "passed_on_head", True)):
        if setup and sh(setup, tree, timeout) != 0:
            print("FAIL setup failed in %s" % tree.name)
            return False, counts
        for path in runnable:
            code = sh(case_cmd.replace("{case}", shlex.quote(path)), tree, timeout)
            good = code == 0 if want else code not in (0, None)
            counts[key] += good
            print("%s %s on %s: %s" % ("OK" if good else "FAIL", path, tree.name,
                                       "timed out" if code is None else "passes" if code == 0 else "fails"))
    counts["suite"] = "pass" if sh(suite_cmd, head, timeout) == 0 else "fail"
    n = counts["cases"]
    ok = counts["failed_on_base"] == n and counts["passed_on_head"] == n and counts["suite"] == "pass"
    return ok, counts


def report(ok, counts):
    n = counts["cases"]
    return ("Learning proof %s: %d of %d added cases fail on base, %d of %d pass on head, %d without an assertion; "
            "full suite on head: %s." % ("passes" if ok else "fails", counts["failed_on_base"], n, counts["passed_on_head"], n,
                                         counts["no_assertion"], counts["suite"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--case-cmd", default="")
    parser.add_argument("--suite-cmd", default="")
    parser.add_argument("--setup", default="")
    parser.add_argument("--summary")
    args = parser.parse_args(argv)
    ok, counts = prove(load(args.files), args.base, args.head, args.case_cmd, args.suite_cmd, args.setup or None)
    line = report(ok, counts)
    print(line)
    if args.summary:
        with open(args.summary, "w") as handle:  # written after all PR code ran, so a planted file is replaced
            handle.write(line + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
