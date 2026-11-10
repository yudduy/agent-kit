"""The gate decision (C5): a pure function of facts read from GitHub. Conclusion is success or failure, never neutral."""

import hashlib
import json
from dataclasses import dataclass, field

from gate.classify import classify, glob_matches
from gate.policy import AUTO_ALLOWED, FLOOR_CLASSES, PolicyError, parse

KIT_VERIFY_WORKFLOW = "yudduy/agent-kit/.github/workflows/verify.yml"
# The gate accepts evidence schema n and n-1 (D11). v1 is the first schema, so n-1 has no format yet.
EVIDENCE_SCHEMAS = (1,)


@dataclass
class Facts:
    repo: str
    pr: dict  # pulls API object
    base_sha: str  # current tip of the base branch
    behind_by: int  # base commits missing from head
    files: list  # compare API files, base...head
    commits: list  # pulls/{n}/commits
    reviews: list  # pulls/{n}/reviews
    checks: dict  # check name -> {app slug -> latest state: success|failure|pending|skipped|neutral|...}
    policy_text: str | None  # .agent-kit/policy.yml at base_sha
    policy_sha: str | None  # its blob sha
    journeys_sha: str | None  # tree sha of .agent-kit/journeys at base_sha
    evidence: bytes | None = None  # evidence.json from the newest verify artifact for head
    artifact_files: dict = field(default_factory=dict)  # artifact member name -> sha256 hex
    attestation: dict | None = None  # set only when `gh attestation verify` passed; see main.verify_attestation
    kit_pins: dict = field(default_factory=dict)  # base workflow path -> refs it pins in `uses: yudduy/agent-kit/...@ref`
    paused: bool = False


def decide(f):
    head = f.pr["head"]["sha"]
    blocking = []
    if f.pr["base"]["ref"] != f.pr["base"]["repo"]["default_branch"]:
        blocking.append(f"only pull requests into {f.pr['base']['repo']['default_branch']} are gated")
    if f.paused:
        blocking.append("the gate is paused (AGENT_KIT_PAUSED)")
    if f.pr["head"]["repo"] is None or f.pr["head"]["repo"]["full_name"] != f.repo:
        blocking.append("pull requests from forks are not supported")
    if len(f.files) >= 300:  # the compare API lists at most 300 files
        blocking.append("the change has 300 or more files, so the gate cannot see all of it")
    if len(f.commits) >= 250:  # the PR commits API lists at most 250, so a hidden committer could approve
        blocking.append("the pull request has 250 or more commits, so the gate cannot check every committer")
    if f.behind_by:
        blocking.append(f"branch is {f.behind_by} commit(s) behind the base branch")
    try:
        if f.policy_text is None:
            raise PolicyError("the base branch has no .agent-kit/policy.yml")
        policy = parse(f.policy_text)
    except PolicyError as error:
        policy = None
        blocking.append(str(error))
    cls, hits = classify(f.files, policy, f.pr["head"]["ref"])

    required = {f"{c['name']} ({c['app']})": f.checks.get(c["name"], {}).get(c["app"], "missing")
                for c in (policy or {}).get("required_checks", [])}
    blocking += [f"required check {name!r} is {state}" for name, state in required.items() if state != "success"]

    if policy is not None:  # D1: every kit caller on base runs the kit the policy pins
        blocking += [f"kit workflow {path} pins {ref}, which does not equal the policy kit_ref"
                     for path, refs in sorted(f.kit_pins.items()) for ref in refs if ref != policy["kit_ref"]]
    if policy is None:
        evidence = "mismatch"
        blocking.append("evidence cannot be checked without a valid base policy")
    else:
        evidence, problems = _check_evidence(f, policy, head)
        blocking += problems

    approval = _approval(f, policy, head) if policy else None
    auto = policy is not None and cls in policy["auto_merge"]
    needs_human = [] if auto or approval else [f"class {cls} needs a human approval on this commit"]
    reasons = blocking + needs_human
    return {
        "v": 1,
        "repo": f.repo,
        "pr": f.pr["number"],
        "head_sha": head,
        "base_sha": f.base_sha,
        "kit_ref": (policy or {}).get("kit_ref"),
        "policy_sha": f.policy_sha,
        "class": cls,
        "classes_hit": hits,
        "evidence": evidence,
        "required_checks": required,
        "approval": approval,
        "conclusion": "failure" if reasons else "success",
        "shadow": "would_not" if blocking else "would_merge",
        "reasons": reasons,
    }


def summary(record):
    """Check-run title and summary; the summary ends with the C5 JSON block."""
    allowed = record["conclusion"] == "success"
    title = "Merge allowed" if allowed else f"Blocked: {record['reasons'][0]}"[:120]
    lines = ["**Merge allowed.**" if allowed else "**Merge blocked.**", ""]
    lines += [f"- {reason}" for reason in record["reasons"]]
    lines.append(f"- Change class: {record['class']}.")
    if record["class"] not in FLOOR_CLASSES + AUTO_ALLOWED:  # code changes such as error fixes
        lines.append(
            "- Shadow verdict: if this class were on the auto list, the gate "
            + ("would merge it." if record["shadow"] == "would_merge" else "would not merge it.")
        )
    lines += ["", "```json", json.dumps(record), "```"]
    return title, "\n".join(lines)


def record_from_summary(text):
    """The C5 record embedded in an earlier agent-gate summary, or None."""
    if not text or "```json\n" not in text:
        return None
    try:
        return json.loads(text.rsplit("```json\n", 1)[1].split("\n```", 1)[0])
    except ValueError:
        return None


def _approval(f, policy, head):
    """D3: a listed User, not the author or any commit author/committer, whose latest review approves head."""
    author = f.pr["user"]["login"].lower()
    writers = {(c.get(role) or {}).get("login", "").lower() for c in f.commits for role in ("author", "committer")}
    approvers = {login.lower() for login in policy["approvers"]}
    latest = {}
    for review in sorted(f.reviews, key=lambda r: r.get("submitted_at") or ""):
        if review["user"] and review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review["user"]["login"].lower()] = review
    for login, review in sorted(latest.items()):
        if (
            review["state"] == "APPROVED"
            and review["commit_id"] == head
            and review["user"]["type"] == "User"
            and login in approvers
            and login != author
            and login not in writers
        ):
            return {"by": review["user"]["login"], "sha": head}
    return None


def _check_evidence(f, policy, head):
    """C2 acceptance rules. Returns (ok|missing|mismatch|unsupported-schema, blocking reasons)."""
    if f.evidence is None:
        return "missing", ["no evidence from the kit verify workflow for this commit"]
    try:
        evidence = json.loads(f.evidence)
        schema = evidence["schema"]
    except (ValueError, TypeError, KeyError):
        return "mismatch", ["evidence.json is not a valid evidence file"]
    if schema not in EVIDENCE_SCHEMAS:
        return "unsupported-schema", [f"evidence schema {schema!r} is not supported; bump kit to a supported version"]
    try:
        problems = _evidence_problems(f, policy, head, evidence)
    except (TypeError, KeyError, AttributeError):
        problems = ["evidence.json is missing required fields"]
    return ("mismatch", problems) if problems else ("ok", [])


def _evidence_problems(f, policy, head, evidence):
    kit_ref = policy["kit_ref"]
    run = evidence["run"]
    attestation = f.attestation or {}
    problems = []
    if not attestation:
        problems.append("evidence has no verified attestation")
    elif (
        attestation["subject_sha256"] != hashlib.sha256(f.evidence).hexdigest()
        or attestation["signer_workflow"] != KIT_VERIFY_WORKFLOW
        or attestation["signer_digest"] != kit_ref
        or not attestation["run_invocation_uri"].startswith(f"https://github.com/{f.repo}/actions/runs/")
        or not attestation["run_invocation_uri"].endswith(f"/actions/runs/{run['id']}/attempts/{run['attempt']}")
    ):
        problems.append("evidence attestation does not match the file or the kit verify run")
    expected = {
        "repo": f.repo,
        "pr": f.pr["number"],
        "head_sha": head,
        "base_sha": f.base_sha,
        "kit_ref": kit_ref,
        "policy_sha": f.policy_sha,
        "journeys_sha": f.journeys_sha,
    }
    problems += [f"evidence {key} does not match" for key, value in expected.items() if evidence[key] != value]
    if run["workflow_ref"] != f"{KIT_VERIFY_WORKFLOW}@{kit_ref}":
        problems.append("evidence was not produced by the pinned kit verify workflow")
    for artifact in evidence["artifacts"]:
        if f.artifact_files.get(artifact["name"]) != artifact["sha256"]:
            problems.append(f"artifact {artifact['name']} hash does not match")
    scenarios = {s["id"]: s for s in evidence["scenarios"]}
    paths = [path for entry in f.files for path in (entry["filename"], entry.get("previous_filename")) if path]
    for journey in policy["journeys"]:
        if not any(glob_matches(area, path) for area in journey["areas"] for path in paths):
            continue
        scenario = scenarios.get(journey["id"])
        if scenario is None or not _scenario_passes(scenario):
            problems.append(f"journey {journey['id']} did not pass on the stored data")
    return problems


def _scenario_passes(scenario):
    assertions = scenario["data_assertions"]
    return (
        scenario["source"] == "base"
        and scenario["ui_result"] == "pass"
        and scenario["result"] == "pass"
        and len(assertions) > 0
        and all(a["pass"] is True for a in assertions)
    )
