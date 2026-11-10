"""agent-kit: onboard a repo, start its journeys run, or bump its kit commit."""
import argparse

from cli import onboard


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-kit", description=__doc__)
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
