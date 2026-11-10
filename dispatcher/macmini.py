"""Helper the Mac mini deciders run. Needs only a fine-grained GitHub token with Issues read/write on agent-control,
plus the Mac mini's own Cursor key for `steer`. It never creates runs (D5).

Run from a kit checkout: `python3 -m dispatcher.macmini <command>`.

  macmini.py heartbeat [--not-accepting] [--open-decisions N]
  macmini.py pending
  macmini.py decide <issue> fix|triage|ignore --prompt-file FILE [--model ID]
  macmini.py steer <issue> --text "..."

Env: AGENT_KIT_CONTROL_REPO (owner/name), AGENT_KIT_HEARTBEAT_ISSUE, GITHUB_TOKEN, CURSOR_API_KEY (steer only).
"""
import argparse
import json
import os
import socket

from dispatcher import cursor, github
from dispatcher import markers as m
from dispatcher.tick import claims_in, current, labels_of, runs_in


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
            if not labels_of(i) & ({m.DECIDED, m.IGNORED, m.CLAIMED, m.RUNNING} | m.DONE)]


def decide(gh, ctl, issue, action, prompt, model=None):
    gh.comment(ctl, issue, m.decision_comment(action, model, prompt))
    gh.add_labels(ctl, issue, [m.IGNORED if action == "ignore" else m.DECIDED])


def steer(gh, client, ctl, issue, text):
    """Send `text` into the active run of the issue's current claim. Refuses anything else, because a steer to an
    idle agent becomes its next run, and only the dispatcher creates runs (D5, INV-8)."""
    comments = current(gh.comments(ctl, issue))
    claims, runs = claims_in(comments), runs_in(comments)
    if not claims or claims[-1]["attempt"] not in runs:
        raise SystemExit("Issue #%d has no dispatcher run for its current claim; only the dispatcher starts runs." % issue)
    cursor_id = runs[claims[-1]["attempt"]]
    before = client.latest_run(cursor_id)
    if before["status"] not in cursor.ACTIVE:
        raise SystemExit("The run for issue #%d is %s, not active; steering it would start a new run." % (issue, before["status"]))
    result = client.steer(cursor_id, text)
    after = client.latest_run(cursor_id) if result.get("delivery") == "queued" else before
    if after["id"] != before["id"]:
        # shortcut: Cursor has no "steer only if running" precondition. A queued steer becomes the agent's next run,
        # so that run is cancelled at once; a steer the running turn accepted is left alone.
        client.cancel(cursor_id, after["id"])
        raise SystemExit("The run for issue #%d ended before the steer landed; cancelled run %s it started." % (issue, after["id"]))
    return result


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
        print(json.dumps(steer(gh, cursor.Client(os.environ["CURSOR_API_KEY"]), ctl, args.issue, args.text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
