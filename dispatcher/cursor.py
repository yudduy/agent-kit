"""The only module that talks to Cursor: Cloud Agents API v1 (beta), https://cursor.com/docs/cloud-agent/api/endpoints.

Endpoints used: POST /v1/agents, GET /v1/agents, GET /v1/agents/{id},
GET /v1/agents/{id}/runs/{runId}, POST /v1/agents/{id}/runs (follow-up or steer).
"""
import base64
import urllib.parse

from dispatcher import http

BASE = "https://api.cursor.com"
ACTIVE = {"CREATING", "RUNNING", "WAITING_FOR_BACKGROUND_WORK"}


class CursorError(Exception):
    def __init__(self, status, data):
        super().__init__("Cursor API returned %s: %s" % (status, (data or {}).get("message") or data))
        self.status, self.data = status, data


class Ambiguous(Exception):
    """A create may have reached Cursor; reconcile with `find` before retrying."""


def agent_id(attempt):
    return "bc-" + attempt


class Client:
    def __init__(self, key, send=http.send):
        self._auth = "Basic " + base64.b64encode((key + ":").encode()).decode()
        self._send = send

    def _call(self, method, path, body=None, query=None):
        url = BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        status, _, data = self._send(method, url, {"Authorization": self._auth}, body)
        if status >= 400:
            raise CursorError(status, data)
        return data

    def create(self, attempt, name, prompt, repo, ref, model=None, work_on_branch=False, auto_pr=True):
        """Create an agent whose id is derived from the attempt, so a repeat POST returns 409, never a twin.
        `ref` None starts from the repo's default branch."""
        body = {"agentId": agent_id(attempt), "name": name[:100], "prompt": {"text": prompt},
                "repos": [{"url": "https://github.com/" + repo, **({"startingRef": ref} if ref else {})}],
                "workOnCurrentBranch": work_on_branch, "autoCreatePR": auto_pr}
        if model:
            body["model"] = {"id": model}
        try:
            return self._call("POST", "/v1/agents", body)["agent"]
        except http.Unreachable as error:
            raise Ambiguous(str(error)) from error
        except CursorError as error:
            if error.status == 409:
                return self.get(agent_id(attempt))
            if error.status >= 500 or error.status == 429:
                raise Ambiguous(str(error)) from error
            raise

    def find(self, attempt, not_before):
        """The agent created for `attempt`, or None. Pages newest first until agents predate the claim."""
        wanted, cursor = agent_id(attempt), None
        while True:
            page = self._call("GET", "/v1/agents", query={"limit": 100, **({"cursor": cursor} if cursor else {})})
            for item in page.get("items", []):
                if item["id"] == wanted or attempt in (item.get("name") or ""):
                    return item
            items, cursor = page.get("items", []), page.get("nextCursor")
            if not cursor or (items and items[-1].get("createdAt", "") < not_before):
                return None

    def get(self, cursor_id):
        return self._call("GET", "/v1/agents/" + cursor_id)

    def latest_run(self, cursor_id):
        agent = self.get(cursor_id)
        return self._call("GET", "/v1/agents/%s/runs/%s" % (cursor_id, agent["latestRunId"]))

    def steer(self, cursor_id, text):
        return self._call("POST", "/v1/agents/%s/runs" % cursor_id, {"prompt": {"text": text}, "delivery": "steer"})
