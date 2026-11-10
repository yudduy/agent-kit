"""Entry point for agent-control's dispatch.yml: `python3 -m dispatcher --config agent-kit.json`."""
import argparse
import json
import os
import uuid
from datetime import datetime, timezone

from dispatcher import cursor, github, sources
from dispatcher.tick import Tick


def pull(repo, settings):
    rows = []
    if "sentry" in settings:
        rows += sources.sentry(settings["sentry"]["org"], settings["sentry"]["project"], os.environ["SENTRY_TOKEN"])
    if "datadog" in settings:
        rows += sources.datadog(settings["datadog"]["query"], os.environ["DATADOG_API_KEY"], os.environ["DATADOG_APP_KEY"],
                                settings["datadog"].get("site", "datadoghq.com"))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="agent-control's agent-kit.json")
    args = parser.parse_args(argv)
    if os.environ.get("AGENT_KIT_PAUSED", "").lower() == "true":
        print("Paused: AGENT_KIT_PAUSED is true, so nothing is dispatched.")
        return 0
    with open(args.config) as handle:
        config = json.load(handle)
    gh = github.GitHub(os.environ["GITHUB_TOKEN"])
    reader = github.GitHub(os.environ["AGENT_KIT_READ_TOKEN"]) if os.environ.get("AGENT_KIT_READ_TOKEN") else gh
    Tick(config, gh, cursor.Client(os.environ["CURSOR_API_KEY"]), pull, now=lambda: datetime.now(timezone.utc),
         new_id=lambda: str(uuid.uuid4()), reader=reader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
