"""Read new production errors from Sentry and Datadog. Read-only; each row becomes one fingerprint."""
import re
import time
import urllib.parse

from dispatcher import http

SENTRY = "https://sentry.io/api/0/organizations/%s/issues/?%s"
DATADOG = "https://api.%s/api/v2/error-tracking/issues/search?include=issue"
DATADOG_LIMIT = 100  # documented maximum issues per search
MAX_PAGES = 10  # per source and repo per tick


class Rows(list):
    """Error rows; `complete` is False when a page or window limit cut the read short."""

    def __init__(self, rows=(), complete=True):
        super().__init__(rows)
        self.complete = complete


def _ok(name, status, data):
    if status != 200:
        raise RuntimeError("%s returned %s" % (name, status))
    return data


def sentry(org, project, token, send=http.send):
    """Unresolved Sentry issues seen in the last 24 h (GET /api/0/organizations/{org}/issues/), following the
    `Link: rel="next"; results="true"` cursor for at most MAX_PAGES pages."""
    query = urllib.parse.urlencode({"project": project, "query": "is:unresolved", "statsPeriod": "24h", "limit": 100})
    url, rows = SENTRY % (org, query), Rows()
    for _ in range(MAX_PAGES):
        status, headers, data = send("GET", url, {"Authorization": "Bearer " + token})
        rows += [{"source": "sentry", "src_id": str(i["id"]), "title": i.get("title") or "Unnamed error",
                  "where": i.get("culprit") or "", "link": i.get("permalink") or "", "count": i.get("count"),
                  "first_seen": i.get("firstSeen") or "", "last_seen": i.get("lastSeen") or ""}
                 for i in _ok("Sentry", status, data)]
        nxt = re.search(r'<([^>]+)>;\s*rel="next";\s*results="true"', (headers or {}).get("Link") or (headers or {}).get("link") or "")
        if not nxt:
            return rows
        url = nxt.group(1)
    rows.complete = False
    return rows


def datadog(query, api_key, app_key, site="datadoghq.com", send=http.send, now=time.time):
    """Datadog Error Tracking issues seen in the last 24 h (POST /api/v2/error-tracking/issues/search).
    The search returns at most 100 issues and has no page cursor, so a full window is split in halves,
    for at most MAX_PAGES requests; an issue found in several windows is kept once."""
    to = int(now() * 1000)
    windows, rows = [(to - 86_400_000, to)], {}
    for _ in range(MAX_PAGES):
        if not windows:
            break
        start, end = windows.pop(0)
        body = {"data": {"type": "search_request", "attributes": {"query": query, "from": start, "to": end, "track": "trace"}}}
        status, _, data = send("POST", DATADOG % site, {"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key}, body)
        data = _ok("Datadog", status, data)
        if len(data.get("data", [])) >= DATADOG_LIMIT and end - start > 1:
            windows += [(start, (start + end) // 2), ((start + end) // 2, end)]
        issues = {i["id"]: i.get("attributes", {}) for i in data.get("included", []) if i.get("type") == "issue"}
        for result in data.get("data", []):
            issue_id = ((result.get("relationships") or {}).get("issue") or {}).get("data", {}).get("id") or result["id"]
            a = issues.get(issue_id, {})
            rows.setdefault(issue_id, {"source": "datadog", "src_id": issue_id,
                                       "title": ": ".join(x for x in (a.get("error_type"), a.get("error_message")) if x) or "Unnamed error",
                                       "where": a.get("service") or "", "link": "https://app.%s/error-tracking?issueId=%s" % (site, issue_id),
                                       "count": (result.get("attributes") or {}).get("total_count"),
                                       "first_seen": _iso(a.get("first_seen")), "last_seen": _iso(a.get("last_seen"))})
    return Rows(rows.values(), complete=not windows)


def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000)) if isinstance(ms, (int, float)) else ""
