#!/usr/bin/env python3
"""INV-13 proof for learning PRs (head branch `learn/*`), run by each repo's `learning-proof` check.

  PYTHONPATH=kit python3 -m learning.proof --files files.jsonl --base base --head head --case-cmd 'python -m pytest {case}'
      --suite-cmd 'python -m pytest' [--setup 'pip install -r requirements.txt'] [--summary "$GITHUB_STEP_SUMMARY"]

A case is each test or eval file the PR adds, or a `.jsonl` case list it appends to (pathcheck's patterns). Each case
is copied into the base tree and run there, then run in the head tree; then the full suite runs on head.
On base the case must fail by an assertion: at least one test fails with an AssertionError (or pytest.fail), and no
test fails any other way, with no collection, setup or teardown error, so a missing import or a syntax error is not
proof. On head every test must pass, with at least one assertion executed. The full suite must pass on head.

Supported runner: pytest only (plain `assert`, or unittest `assert*` methods run under pytest). The case command must
run pytest; the kit's plugin (`learning/pytest_plugin/agent_kit_proof.py`, loaded through PYTEST_ADDOPTS) reports
each outcome and the assertions passed. A command that writes no report (Jest, Vitest, Playwright, plain scripts)
fails the proof: their JSON reporters give no executed-assertion count to check.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from fnmatch import fnmatch
from pathlib import Path

from learning.pathcheck import CASES, HOOKS, load

PLUGIN = Path(__file__).with_name("pytest_plugin")
NO_ASSERTION = "passes without executing an assertion"


def cases(files):
    out = []
    for f in files:
        path = f["filename"]
        if any(fnmatch(path, p) for p in HOOKS) or not any(fnmatch(path, p) for p in CASES):
            continue
        if f["status"] == "added" or (f["status"] == "modified" and path.endswith(".jsonl")):
            out.append(path)
    return out


def sh(command, cwd, timeout, env=None):
    print("$ (%s) %s" % (cwd.name, command))
    try:
        return subprocess.run(command, shell=True, cwd=cwd, timeout=timeout, env=env).returncode
    except subprocess.TimeoutExpired:
        print("timed out after %ds" % timeout)
        return None


def run_case(command, tree, timeout):
    """(exit code, pytest report or None) for one case run with the kit's plugin loaded."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        env = dict(os.environ, AGENT_KIT_PROOF_REPORT=str(out),
                   PYTHONPATH=os.pathsep.join(filter(None, [str(PLUGIN), os.environ.get("PYTHONPATH")])),
                   PYTEST_ADDOPTS=" ".join(filter(None, [os.environ.get("PYTEST_ADDOPTS"),
                                                         "-p agent_kit_proof -o enable_assertion_pass_hook=true"])))
        code = sh(command, tree, timeout, env)
        return code, json.loads(out.read_text()) if out.exists() else None


def verdict(code, result, on_base):
    """'' when the run proves its side, else the reason it does not."""
    if result is None:
        return "timed out" if code is None else "wrote no pytest report (only pytest is supported)"
    outcomes = [t["outcome"] for t in result["tests"]]
    if result["errors"]:
        return "collection, setup or teardown error: " + "; ".join(result["errors"][:3])
    if on_base:
        if "assertion" not in outcomes or set(outcomes) - {"assertion", "passed"} or code == 0:
            return "does not fail by an assertion (outcomes: %s)" % (", ".join(outcomes) or "no tests")
        return ""
    if not outcomes or set(outcomes) != {"passed"} or code != 0:
        return "does not pass (outcomes: %s)" % (", ".join(outcomes) or "no tests")
    return "" if result["assertions"] else NO_ASSERTION


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
    for path in found:
        (base / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(head / path, base / path)
    # Every base run ends before head setup or head runs start, so code only on head cannot change the base results.
    for tree, key, on_base in ((base, "failed_on_base", True), (head, "passed_on_head", False)):
        if setup and sh(setup, tree, timeout) != 0:
            print("FAIL setup failed in %s" % tree.name)
            return False, counts
        for path in found:
            code, result = run_case(case_cmd.replace("{case}", shlex.quote(path)), tree, timeout)
            reason = verdict(code, result, on_base)
            counts[key] += not reason
            counts["no_assertion"] += reason == NO_ASSERTION
            print("%s %s on %s%s" % ("FAIL" if reason else "OK", path, tree.name, ": " + reason if reason else ""))
    counts["suite"] = "pass" if sh(suite_cmd, head, timeout) == 0 else "fail"
    n = counts["cases"]
    ok = counts["failed_on_base"] == n and counts["passed_on_head"] == n and counts["suite"] == "pass"
    return ok, counts


def report(ok, counts):
    n = counts["cases"]
    return ("Learning proof %s: %d of %d added cases fail on base, %d of %d pass on head, %d pass on head without executing an assertion; "
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
