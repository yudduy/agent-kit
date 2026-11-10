"""Read the two C1 policy keys the dispatcher needs (`caps`, `approvers`) from a repo's base policy.yml.

Only top-level flow values (`caps: {a: 1}`, `approvers: [x]`) are read; that is the form C1 documents.
Replace with the gate's YAML reader once both land on main.
"""
import re


def _scalar(text):
    text = text.strip().strip("'\"")
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


def flow(policy_text, key):
    match = re.search(r"^%s:\s*([\[{].*?[\]}])\s*(?:#.*)?$" % re.escape(key), policy_text or "", re.M)
    if not match:
        return None
    inner = match.group(1)[1:-1]
    items = [p.strip() for p in inner.split(",") if p.strip()]
    if match.group(1).startswith("["):
        return [_scalar(i) for i in items]
    return {k.strip(): _scalar(v) for k, v in (i.split(":", 1) for i in items)}


def caps(policy_text, kit):
    """Kit caps lowered by the repo's caps; a repo can never raise one."""
    repo = flow(policy_text, "caps")
    repo = repo if isinstance(repo, dict) else {}
    return {k: min(v, repo[k]) if isinstance(repo.get(k), (int, float)) else v for k, v in kit.items()}
