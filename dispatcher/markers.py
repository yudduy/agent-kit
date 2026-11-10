"""C3 intake labels and markers v1, C4 heartbeat v1 (docs/design/2026-11-10-agent-kit.md)."""
import hashlib
import json
import re

INCIDENT, NEEDS_DECISION, DECIDED = "agent:incident", "agent:needs-decision", "agent:decided"
CLAIMED, RUNNING, PR_OPEN = "agent:claimed", "agent:running", "agent:pr-open"
TRIAGED, IGNORED, NEEDS_HUMAN, PAID_OK = "agent:triaged", "agent:ignored", "agent:needs-human", "agent:paid-ok"
DONE = {PR_OPEN, TRIAGED, IGNORED, NEEDS_HUMAN}

_MARKER = re.compile(r"<!-- agent-kit:(intake|decision|claim|run|heartbeat) v1 (.*?) -->", re.S)


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
    if (comment.get("user") or {}).get("login") not in deciders:
        return None
    body = comment.get("body") or ""
    fields = parse("decision", body)
    if not fields or fields.get("action") not in ("fix", "triage", "ignore"):
        return None
    prompt = _MARKER.sub("", body).strip()
    if fields.get("action") != "ignore" and hashlib.sha256(prompt.encode()).hexdigest() != fields.get("prompt_sha256"):
        return None
    return {**fields, "prompt": prompt}


def decision_comment(action, model, prompt):
    payload = {"action": action, "model": model, "prompt_sha256": hashlib.sha256(prompt.strip().encode()).hexdigest()}
    return json_marker("decision", payload) + "\n" + prompt.strip()
