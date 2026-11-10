"""Helper the Mac mini deciders run. Needs only a fine-grained GitHub token with Issues read/write on agent-control.
It never calls Cursor: a steer is a request comment that the dispatcher carries out on its next tick (D5).

Run from a kit checkout: `python3 -m dispatcher.macmini <command>`.

  macmini.py heartbeat [--not-accepting] [--open-decisions N]
  macmini.py pending
  macmini.py decide <issue> fix|triage|ignore --prompt-file FILE [--model ID]
  macmini.py steer <issue> --text "..."

Env: AGENT_KIT_CONTROL_REPO (owner/name), AGENT_KIT_HEARTBEAT_ISSUE, GITHUB_TOKEN.
"""
import argparse
import json
import os
import socket

from dispatcher import github
from dispatcher import markers as m
from dispatcher.tick import labels_of


def heartbeat(gh, ctl, issue, accepting=True, open_decisions=0, deciders=("claude", "codex", "opencode")):
    """C4: edit this login's heartbeat comment in place, or post the first one."""
    me = gh.whoami()
    body = m.json_marker("heartbeat", {"host": socket.gethostname().split(".")[0], "accepting": accepting,
                                       "deciders": list(deciders), "open_decisions": open_decisions})
    mine = [c for c in gh.comments(ctl, issue) if (c.get("user") or {}).get("login") == me and m.parse("heartbeat", c["body"])]
    return gh.edit_comment(ctl, mine[0]["id"], body) if mine else gh.comment(ctl, issue, body)


def pending(gh, ctl):
    """Open incidents waiting for a decision."""
    return [i for i in gh.issues(ctl, m.NEEDS_DECISION)
            if not labels_of(i) & {m.DECIDED, m.IGNORED, *m.DISPATCHER_LABELS}]


def decide(gh, ctl, issue, action, prompt, model=None):
    gh.comment(ctl, issue, m.decision_comment(action, model, prompt))
    gh.add_labels(ctl, issue, [m.IGNORED if action == "ignore" else m.DECIDED])


def steer(gh, ctl, issue, text):
    """Ask the dispatcher to steer the issue's current run; it refuses when no run is active."""
    return gh.comment(ctl, issue, m.steer_comment(text))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    beat = sub.add_parser("heartbeat")
    beat.add_argument("--not-accepting", action="store_true")
    beat.add_argument("--open-decisions", type=int, default=0)
    sub.add_parser("pending")
    dec = sub.add_parser("decide")
    dec.add_argument("issue", type=int)
    dec.add_argument("action", choices=["fix", "triage", "ignore"])
    dec.add_argument("--prompt-file", required=True)
    dec.add_argument("--model")
    st = sub.add_parser("steer")
    st.add_argument("issue", type=int)
    st.add_argument("--text", required=True)
    args = parser.parse_args(argv)
    ctl, gh = os.environ["AGENT_KIT_CONTROL_REPO"], github.GitHub(os.environ["GITHUB_TOKEN"])
    if args.command == "heartbeat":
        heartbeat(gh, ctl, int(os.environ["AGENT_KIT_HEARTBEAT_ISSUE"]), not args.not_accepting, args.open_decisions)
    elif args.command == "pending":
        for issue in pending(gh, ctl):
            print(json.dumps({"number": issue["number"], "title": issue["title"], "intake": m.parse("intake", issue["body"])}))
    elif args.command == "decide":
        with open(args.prompt_file) as handle:
            decide(gh, ctl, args.issue, args.action, handle.read(), args.model)
    else:
        steer(gh, ctl, args.issue, args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
