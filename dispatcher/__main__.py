"""Entry point for agent-control's dispatch.yml: `python3 -m dispatcher --control <agent-control checkout>`."""
import argparse
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dispatcher import cursor, github, sources
from dispatcher.tick import Tick


def pull(repo, settings):
    """Rows from every configured provider. A provider that fails is logged and makes the read incomplete,
    so the other providers' rows still count and no incident is idle-closed on a partial read."""
    readers = []
    if "sentry" in settings:
        readers.append(("Sentry", lambda: sources.sentry(settings["sentry"]["org"], settings["sentry"]["project"], os.environ["SENTRY_TOKEN"])))
    if "datadog" in settings:
        readers.append(("Datadog", lambda: sources.datadog(settings["datadog"]["query"], os.environ["DATADOG_API_KEY"],
                                                           os.environ["DATADOG_APP_KEY"], settings["datadog"].get("site", "datadoghq.com"))))
    rows = sources.Rows()
    for name, read in readers:
        try:
            found = read()
        except Exception as error:  # one provider down must not hide the others
            print("Could not read %s errors for %s: %s" % (name, repo, error))
            rows.complete = False
            continue
        rows += found
        rows.complete = rows.complete and found.complete
    return rows


def load(control, control_repo):
    """The gate's registry (`agent-kit/repos.json`) plus the dispatcher settings (`agent-kit/dispatch.json`)."""
    registry = json.loads((Path(control) / "agent-kit" / "repos.json").read_text())
    repos = registry.get("repos") if registry.get("v") == 1 else None
    if not isinstance(repos, list) or not all(isinstance(r, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", r) for r in repos):
        raise ValueError('agent-kit/repos.json must be {"v": 1, "repos": ["owner/name", ...]}')
    settings = json.loads((Path(control) / "agent-kit" / "dispatch.json").read_text())
    if settings.pop("v", None) != 1:
        raise ValueError('agent-kit/dispatch.json must have "v": 1')
    return {**settings, "control_repo": control_repo, "repos": repos}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", required=True, help="checkout of agent-control's main branch")
    args = parser.parse_args(argv)
    if os.environ.get("AGENT_KIT_PAUSED", "").lower() == "true":
        print("Paused: AGENT_KIT_PAUSED is true, so nothing is dispatched.")
        return 0
    config = load(args.control, os.environ["GITHUB_REPOSITORY"])
    gh = github.GitHub(os.environ["GITHUB_TOKEN"])
    reader = github.GitHub(os.environ["AGENT_KIT_READ_TOKEN"]) if os.environ.get("AGENT_KIT_READ_TOKEN") else gh
    Tick(config, gh, cursor.Client(os.environ["CURSOR_API_KEY"]), pull, now=lambda: datetime.now(timezone.utc),
         new_id=lambda: str(uuid.uuid4()), reader=reader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
