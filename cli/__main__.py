"""agent-kit: onboard a repo, start its journeys run, bump its kit commit, scan it, or rerun its verification."""
import argparse
import importlib
import sys
from pathlib import Path

from cli import onboard


def kit_module(name, pr):
    """A kit module that arrives in another PR; the CLI still runs without it."""
    try:
        return importlib.import_module(name)
    except ImportError:
        raise SystemExit("`%s` is not in this kit checkout yet (it arrives with PR #%d); update the kit checkout." % (name, pr))


def verify(argv):
    parser = argparse.ArgumentParser(prog="agent-kit verify", description="Rerun the kit's verification of one commit locally.")
    parser.add_argument("--sha", required=True, help="head commit to verify")
    parser.add_argument("--scenario", action="append", default=[], help="journey id to run (repeatable; default all)")
    parser.add_argument("--base", help="base commit (default: merge base of --sha and origin/HEAD)")
    parser.add_argument("--repo", default=".", help="repo checkout holding both commits")
    parser.add_argument("--out", help="output folder (default: agent-kit-evidence/<sha>)")
    args, extra = parser.parse_known_args(argv)
    local_run = kit_module("verify.local_run", 3)
    base = args.base or onboard.run("git", "merge-base", args.sha, "origin/HEAD", cwd=args.repo)
    out = args.out or str(Path("agent-kit-evidence") / args.sha[:12])
    scenarios = [x for s in args.scenario for x in ("--scenario", s)]
    return local_run.main(["--repo", args.repo, "--head", args.sha, "--base", base, "--out", out] + scenarios + extra)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["doctor"]:
        return kit_module("doctor.doctor", 4).main(argv[1:])
    if argv[:1] == ["verify"]:
        return verify(argv[1:])
    parser = argparse.ArgumentParser(prog="agent-kit", description=__doc__ + " Also: `agent-kit doctor [repo]`, "
                                     "`agent-kit verify --sha <head> --scenario <id>`.")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="open one draft onboarding PR and start the journeys run")
    init.add_argument("repo", help="owner/name")
    init.add_argument("--ref", help="kit commit to pin (default: this checkout's HEAD, which must be on origin/main)")
    init.add_argument("--dry-run", action="store_true", help="print the planned files and PR; touch nothing")
    init.add_argument("--force", action="store_true", help="overwrite an existing .agent-kit/")
    journeys = sub.add_parser("journeys", help="start the Cursor run that proposes journeys on the onboarding branch")
    journeys.add_argument("repo")
    sync = sub.add_parser("sync", help="open a PR that bumps the repo's kit commit")
    sync.add_argument("repo")
    sync.add_argument("--ref")
    args = parser.parse_args(argv)
    if args.command == "init":
        return onboard.init(args.repo, args.ref, args.dry_run, args.force)
    if args.command == "journeys":
        onboard.start_journeys(args.repo)
        return 0
    return onboard.sync(args.repo, args.ref)


if __name__ == "__main__":
    raise SystemExit(main())
