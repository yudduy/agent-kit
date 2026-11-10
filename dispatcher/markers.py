"""C3 intake labels and markers v1, C4 heartbeat v1 (docs/design/2026-11-10-agent-kit.md).

Labels are display only; the dispatcher repairs its own each tick. Run state comes from markers the dispatcher
writes (claim, run, result, escalate, reopen, idle, steered) and requests deciders write (decision, steer).
"""
import hashlib
import json
import re
import uuid

INCIDENT, NEEDS_DECISION, DECIDED = "agent:incident", "agent:needs-decision", "agent:decided"
CLAIMED, RUNNING, PR_OPEN = "agent:claimed", "agent:running", "agent:pr-open"
TRIAGED, IGNORED, NEEDS_HUMAN, PAID_OK = "agent:triaged", "agent:ignored", "agent:needs-human", "agent:paid-ok"
DISPATCHER_LABELS = (CLAIMED, RUNNING, PR_OPEN, TRIAGED, NEEDS_HUMAN)

_MARKER = re.compile(r"<!-- agent-kit:(intake|decision|claim|run|result|escalate|heartbeat|reopen|idle|steer|steered) v1 (.*?) -->", re.S)


def fingerprint(source, src_id):
    return hashlib.sha256(("%s:%s" % (source, src_id)).encode()).hexdigest()[:16]


def intake(fp, source, src_id, first_seen, repo):
    # `repo:` is an additive field: incident issues live in agent-control, so the marker names the app repo.
    return "<!-- agent-kit:intake v1 fp:%s source:%s src-id:%s first-seen:%s repo:%s -->" % (fp, source, src_id, first_seen, repo)


def json_marker(kind, payload):
    return "<!-- agent-kit:%s v1 %s -->" % (kind, json.dumps(payload, separators=(",", ":")))


def parse(kind, text):
    """Fields of the first `kind` marker in text, or None. Intake markers are key:value tokens; others are JSON."""
    for found, raw in _MARKER.findall(text or ""):
        if found != kind:
            continue
        if kind == "intake":
            return dict(token.split(":", 1) for token in raw.split() if ":" in token)
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return None


def decision(comment, deciders):
    """A valid decision from a decider login: action, model and the prompt that follows the marker."""
    found = _signed("decision", comment, deciders)
    return found if found and found.get("action") in ("fix", "triage", "ignore") else None


def decision_comment(action, model, prompt):
    payload = {"action": action, "model": model, "prompt_sha256": hashlib.sha256(prompt.strip().encode()).hexdigest()}
    return json_marker("decision", payload) + "\n" + prompt.strip()


def _signed(kind, comment, deciders):
    """Fields and the text after a `kind` marker, from a decider login, with an intact prompt hash; else None."""
    if (comment.get("user") or {}).get("login") not in deciders:
        return None
    body = comment.get("body") or ""
    fields = parse(kind, body)
    if not isinstance(fields, dict):
        return None
    prompt = _MARKER.sub("", body).strip()
    return {**fields, "prompt": prompt} if hashlib.sha256(prompt.encode()).hexdigest() == fields.get("prompt_sha256") else None


def steer_comment(text):
    """A decider's request that the dispatcher steer the current run (the Mac mini never calls Cursor)."""
    payload = {"id": str(uuid.uuid4()), "prompt_sha256": hashlib.sha256(text.strip().encode()).hexdigest()}
    return json_marker("steer", payload) + "\n" + text.strip()


def steer_request(comment, deciders):
    found = _signed("steer", comment, deciders)
    return found if found and found.get("id") else None
