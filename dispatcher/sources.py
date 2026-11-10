"""Read new production errors from Sentry and Datadog. Read-only; each row becomes one fingerprint."""
import time
import urllib.parse

from dispatcher import http

SENTRY = "https://sentry.io/api/0/organizations/%s/issues/?%s"
DATADOG = "https://api.%s/api/v2/error-tracking/issues/search?include=issue"


def _ok(name, status, data):
    if status != 200:
        raise RuntimeError("%s returned %s" % (name, status))
    return data


def sentry(org, project, token, send=http.send):
    """Unresolved Sentry issues seen in the last 24 h (GET /api/0/organizations/{org}/issues/)."""
    query = urllib.parse.urlencode({"project": project, "query": "is:unresolved", "statsPeriod": "24h", "limit": 100})
    status, _, data = send("GET", SENTRY % (org, query), {"Authorization": "Bearer " + token})
    return [{"source": "sentry", "src_id": str(i["id"]), "title": i.get("title") or "Unnamed error",
             "where": i.get("culprit") or "", "link": i.get("permalink") or "", "count": i.get("count"),
             "first_seen": i.get("firstSeen") or "", "last_seen": i.get("lastSeen") or ""}
            for i in _ok("Sentry", status, data)]


def datadog(query, api_key, app_key, site="datadoghq.com", send=http.send, now=time.time):
    """Datadog Error Tracking issues seen in the last 24 h (POST /api/v2/error-tracking/issues/search)."""
    to = int(now() * 1000)
    body = {"data": {"type": "search_request", "attributes": {"query": query, "from": to - 86_400_000, "to": to, "track": "trace"}}}
    status, _, data = send("POST", DATADOG % site, {"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key}, body)
    data = _ok("Datadog", status, data)
    issues = {i["id"]: i.get("attributes", {}) for i in data.get("included", []) if i.get("type") == "issue"}
    rows = []
    for result in data.get("data", []):
        issue_id = ((result.get("relationships") or {}).get("issue") or {}).get("data", {}).get("id") or result["id"]
        a = issues.get(issue_id, {})
        rows.append({"source": "datadog", "src_id": issue_id,
                     "title": ": ".join(x for x in (a.get("error_type"), a.get("error_message")) if x) or "Unnamed error",
                     "where": a.get("service") or "", "link": "https://app.%s/error-tracking?issueId=%s" % (site, issue_id),
                     "count": (result.get("attributes") or {}).get("total_count"),
                     "first_seen": _iso(a.get("first_seen")), "last_seen": _iso(a.get("last_seen"))})
    return rows


def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000)) if isinstance(ms, (int, float)) else ""
