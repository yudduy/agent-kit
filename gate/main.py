"""Actions entrypoint run inside agent-control: decide every open PR in every registered repo (D2, C5).

Reads everything through the API and never checks out or runs PR code (INV-2). Policy, journeys and
evidence inputs come from the base branch tip, never the PR head (INV-3).

    python -m gate.main                     # live; needs AGENT_GATE_APP_ID, AGENT_GATE_PRIVATE_KEY, GITHUB_TOKEN
    python -m gate.main --dry-run DIR       # read JSON fixtures from DIR, print the writes instead of sending them
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import traceback
import zipfile
from urllib.parse import quote

from gate.decide import Facts, decide, record_from_summary, regression_counts, summary
from gate.github import Fixture, GitHub, NotFound, installation_client
from gate.policy import PolicyError, parse

CONFIG_PATH = "agent-kit/repos.json"  # in agent-control, read from main
POLICY_PATH = ".agent-kit/policy.yml"
JOURNEYS_DIR = ".agent-kit/journeys"  # its tree sha is listed in .agent-kit
EVIDENCE_ARTIFACT = "agent-kit-evidence"  # Actions artifact holding evidence.json and the files it lists
MAX_ARTIFACT_BYTES = 200_000_000  # shortcut: larger artifacts count as missing evidence; raise if real traces exceed it
LOG_BRANCH = "decisions-log"
CHECK_NAME = "agent-gate"
KIT_USES = re.compile(r"""(['"]?)yudduy/agent-kit(?:/[^@\s'"]*)?@([^ \t\r\n]*)""", re.IGNORECASE)  # any kit reference; the ref runs to YAML whitespace
REPO_NAME = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def run(client_for, control, control_repo, paused, app_id, day):
    """One gate tick. client_for(repo) returns a client for that repo; control writes the decisions log.

    Returns the number of repos and PRs that raised an error; each keeps its previous check.
    """
    errors = 0
    for repo in load_config(control, control_repo):
        try:
            client = client_for(repo)
            prs = client.pages(f"/repos/{repo}/pulls?state=open")
        except Exception:
            errors += 1
            print(f"{repo}: cannot list pull requests", file=sys.stderr)
            traceback.print_exc()
            continue
        for pr in prs:
            try:
                gate_pr(client, repo, pr, paused, app_id, lambda record: append_log(control, control_repo, record, day))
            except Exception:
                errors += 1
                print(f"{repo}#{pr['number']}: gate error", file=sys.stderr)
                traceback.print_exc()
    return errors


def load_config(control, control_repo):
    data = json.loads(_file(control, control_repo, CONFIG_PATH, "main")[0])
    repos = data.get("repos") if data.get("v") == 1 else None
    if not isinstance(repos, list) or not all(isinstance(r, str) and REPO_NAME.match(r) for r in repos):
        raise ValueError(f"{CONFIG_PATH} must be {{\"v\": 1, \"repos\": [\"owner/name\", ...]}}")
    return repos


def gate_pr(client, repo, pr, paused, app_id, log):
    """Decide one PR. A changed decision is logged first, then posted; then a passing PR is merged."""
    facts, previous = gather(client, repo, pr, paused, app_id)
    record = decide(facts)
    if record != previous:
        log(record)
        title, text = summary(record, regression_counts(facts))
        client.post(f"/repos/{repo}/check-runs", {
            "name": CHECK_NAME,
            "head_sha": record["head_sha"],
            "status": "completed",
            "conclusion": record["conclusion"],
            "output": {"title": title, "summary": text},
        })
    if record["conclusion"] == "success" and not pr["draft"]:  # D2: the gate merges every passing PR
        client.put(f"/repos/{repo}/pulls/{pr['number']}/merge", {"sha": record["head_sha"]})


def gather(client, repo, pr, paused, app_id):
    number, head = pr["number"], pr["head"]["sha"]
    base_sha = client.get(f"/repos/{repo}/branches/{quote(pr['base']['ref'], safe='')}")["commit"]["sha"]
    compare = client.get(f"/repos/{repo}/compare/{base_sha}...{head}")
    try:
        policy_text, policy_sha = _file(client, repo, POLICY_PATH, base_sha)
    except NotFound:
        policy_text, policy_sha = None, None
    try:
        listing = client.get(f"/repos/{repo}/contents/.agent-kit?ref={base_sha}")
        journeys_sha = next((e["sha"] for e in listing if e["path"] == JOURNEYS_DIR and e["type"] == "dir"), None)
    except NotFound:
        journeys_sha = None

    checks, previous = {}, None
    for check in sorted(client.pages(f"/repos/{repo}/commits/{head}/check-runs", "check_runs"), key=lambda c: c["id"]):
        app = check.get("app") or {}
        if check["name"] == CHECK_NAME:
            if app_id is not None and app.get("id") == app_id:
                previous = record_from_summary(check["output"]["summary"])
            continue
        state = check["conclusion"] if check["status"] == "completed" else "pending"
        checks.setdefault(check["name"], {})[app.get("slug")] = state  # latest run per name and app

    facts = Facts(
        repo=repo,
        pr=pr,
        base_sha=base_sha,
        behind_by=compare["behind_by"],
        files=compare["files"],
        commits=client.pages(f"/repos/{repo}/pulls/{number}/commits"),
        reviews=client.pages(f"/repos/{repo}/pulls/{number}/reviews"),
        checks=checks,
        policy_text=policy_text,
        policy_sha=policy_sha,
        journeys_sha=journeys_sha,
        paused=paused,
        kit_pins=_kit_pins(client, repo, base_sha),
    )
    _attach_evidence(client, facts, _kit_ref(policy_text))
    return facts, previous


def _attach_evidence(client, facts, kit_ref):
    """Newest evidence artifact for the head SHA, its file hashes, and its attestation."""
    head = facts.pr["head"]["sha"]
    runs = client.get(f"/repos/{facts.repo}/actions/runs?head_sha={head}&event=pull_request&per_page=100")["workflow_runs"]
    for workflow_run in sorted(runs, key=lambda r: r["id"], reverse=True):
        artifacts = client.get(f"/repos/{facts.repo}/actions/runs/{workflow_run['id']}/artifacts?name={EVIDENCE_ARTIFACT}")
        artifact = next((a for a in artifacts["artifacts"] if a["name"] == EVIDENCE_ARTIFACT and not a["expired"] and a["size_in_bytes"] <= MAX_ARTIFACT_BYTES), None)
        if artifact:
            try:
                files = client.artifact_files(facts.repo, artifact["id"], MAX_ARTIFACT_BYTES)
            except (zipfile.BadZipFile, RuntimeError, OSError):
                return
            facts.evidence = files.get("evidence.json")
            facts.artifact_files = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
            if facts.evidence is not None and kit_ref:
                facts.attestation = client.verify_attestation(facts.repo, facts.evidence, kit_ref)
            return


def _kit_pins(client, repo, base_sha):
    """Refs pinned by each base workflow that calls the kit (D1)."""
    try:
        listing = client.get(f"/repos/{repo}/contents/.github/workflows?ref={base_sha}")
    except NotFound:
        return {}
    pins = {}
    for entry in listing:
        if entry["type"] == "file" and entry["name"].endswith((".yml", ".yaml")):
            # Undo the YAML escapes that keep text readable (escaped line breaks, `\/`) before scanning.
            text = re.sub(r"\\(?:\r\n|\r|\n)[ \t]*", "", _file(client, repo, entry["path"], base_sha)[0]).replace("\\/", "/")
            refs = [ref[:-1] if quote and ref.endswith(quote) else ref for quote, ref in KIT_USES.findall(text)]
            if re.search(r"\\[xuU]", text):  # shortcut: a text scan cannot decode YAML escapes, so fail closed
                refs.append("an unreadable ref (YAML escape)")
            if refs:
                pins[entry["path"]] = refs
    return pins


def _kit_ref(policy_text):
    try:
        return parse(policy_text)["kit_ref"] if policy_text is not None else None
    except PolicyError:
        return None


def _file(client, repo, path, ref):
    data = client.get(f"/repos/{repo}/contents/{path}?ref={ref}")
    return base64.b64decode(data["content"]).decode(), data["sha"]


def append_log(control, control_repo, record, day):
    """Append the decision as one JSON line to decisions/<day>.jsonl on the decisions-log branch (D11)."""
    path = f"/repos/{control_repo}/contents/decisions/{day}.jsonl"
    try:
        current = control.get(f"{path}?ref={LOG_BRANCH}")
        old, sha = base64.b64decode(current["content"]), current["sha"]
    except NotFound:
        old, sha = b"", None
    line = (json.dumps(record) + "\n").encode()
    body = {"message": f"Log gate decision for {record['repo']}#{record['pr']}", "content": base64.b64encode(old + line).decode(), "branch": LOG_BRANCH}
    if sha:
        body["sha"] = sha
    control.put(path, body)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m gate.main", description="Run one agent-gate tick.")
    parser.add_argument("--dry-run", metavar="DIR", help="read API responses from fixture DIR and print writes")
    args = parser.parse_args(argv)
    paused = os.environ.get("AGENT_KIT_PAUSED", "").strip().lower() == "true"
    control_repo = os.environ.get("GITHUB_REPOSITORY", "yudduy/agent-control")
    app_id = int(os.environ["AGENT_GATE_APP_ID"]) if os.environ.get("AGENT_GATE_APP_ID") else None
    day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if args.dry_run:
        fixture = Fixture(args.dry_run)
        errors = run(lambda repo: fixture, fixture, control_repo, paused, app_id, day)
        for method, path, body in fixture.writes:
            print(json.dumps({"method": method, "path": path, "body": body}))
    else:
        key = os.environ["AGENT_GATE_PRIVATE_KEY"]
        if app_id is None:
            raise SystemExit("AGENT_GATE_APP_ID is required")
        control = GitHub(os.environ["GITHUB_TOKEN"])
        errors = run(lambda repo: installation_client(app_id, key, repo), control, control_repo, paused, app_id, day)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
