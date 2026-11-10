"""In-memory GitHub and a Cursor server that answers with the recorded fixture shapes. No network."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import http

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class Clock:
    def __init__(self, start="2026-11-10T03:05:00+00:00"):
        self.t = datetime.fromisoformat(start)

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += timedelta(minutes=minutes)

    def iso(self):
        return self.t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    def __init__(self, clock, login="dispatcher"):
        self.clock, self.login = clock, login
        self.issues_, self.comments_, self.events_, self.files = {}, {}, {}, {}
        self.assigned, self._next = {}, 1000

    def _id(self):
        self._next += 1
        return self._next

    def whoami(self):
        return self.login

    def issues(self, repo, label, state="open"):
        return [copy.deepcopy(i) for i in self.issues_.values()
                if any(l["name"] == label for l in i["labels"]) and state in ("all", i["state"])]

    def create_issue(self, repo, title, body, labels):
        number = len(self.issues_) + 1
        issue = {"number": number, "title": title, "body": body, "state": "open", "closed_at": None, "user": {"login": self.login},
                 "labels": [{"name": l} for l in labels], "created_at": self.clock.iso(), "updated_at": self.clock.iso()}
        self.issues_[number] = issue
        return copy.deepcopy(issue)

    def update_issue(self, repo, number, **fields):
        self.issues_[number].update(fields)
        self.issues_[number]["updated_at"] = self.clock.iso()

    def labels(self, number):
        return {l["name"] for l in self.issues_[number]["labels"]}

    def add_labels(self, repo, number, labels):
        for label in labels:
            if label not in self.labels(number):
                self.issues_[number]["labels"].append({"name": label})
        self.issues_[number]["updated_at"] = self.clock.iso()

    def remove_label(self, repo, number, label):
        self.issues_[number]["labels"] = [l for l in self.issues_[number]["labels"] if l["name"] != label]

    def assign(self, repo, number, logins):
        self.assigned.setdefault(number, []).extend(logins)

    def comments(self, repo, number):
        return copy.deepcopy(self.comments_.get(number, []))

    def comment(self, repo, number, body, login=None):
        c = {"id": self._id(), "body": body, "user": {"login": login or self.login},
             "created_at": self.clock.iso(), "updated_at": self.clock.iso()}
        self.comments_.setdefault(number, []).append(c)
        if number in self.issues_:
            self.issues_[number]["updated_at"] = self.clock.iso()
        return copy.deepcopy(c)

    def edit_comment(self, repo, comment_id, body):
        for c in (c for cs in self.comments_.values() for c in cs if c["id"] == comment_id):
            c.update(body=body, updated_at=self.clock.iso())
            return copy.deepcopy(c)

    def events(self, repo, number):
        return self.events_.get(number, [])

    def file(self, repo, path, ref):
        return self.files.get((repo, path))

    def claims(self, number):
        return [c for c in self.comments_.get(number, []) if "agent-kit:claim" in c["body"]]


class FakeCursor:
    """A `send` for dispatcher.cursor.Client. `lose_create` drops the response after the agent exists;
    `fail_create` drops the request before it reaches the server."""

    def __init__(self, clock):
        self.clock, self.agents, self.creates = clock, {}, 0
        self.run_status, self.lose_create, self.fail_create = "RUNNING", False, False

    def __call__(self, method, url, headers=None, body=None):
        path = url.split("api.cursor.com", 1)[1].split("?")[0]
        if method == "POST" and path == "/v1/agents":
            if self.fail_create:
                raise http.Unreachable("connection reset")
            if body["agentId"] in self.agents:
                return 409, {}, {"code": "agent_id_conflict"}
            self.creates += 1
            created = fixture("cursor_create.json")
            agent = {**created["agent"], "id": body["agentId"], "name": body["name"], "createdAt": self.clock.iso(),
                     "url": "https://cursor.com/agents/" + body["agentId"], "body": body}
            self.agents[body["agentId"]] = agent
            if self.lose_create:
                raise http.Unreachable("read timed out")
            return 201, {}, {"agent": agent, "run": created["run"]}
        if method == "GET" and path == "/v1/agents":
            items = sorted(self.agents.values(), key=lambda a: a["createdAt"], reverse=True)
            return 200, {}, {"items": items}
        parts = path.strip("/").split("/")
        if method == "GET" and len(parts) == 3:
            return (200, {}, self.agents[parts[2]]) if parts[2] in self.agents else (404, {}, {"code": "not_found"})
        if method == "GET" and len(parts) == 5 and parts[3] == "runs":
            run = {**fixture("cursor_run_finished.json"), "agentId": parts[2], "status": self.run_status}
            if self.run_status != "FINISHED":
                run.pop("git")
            return 200, {}, run
        raise AssertionError("unexpected Cursor call %s %s" % (method, url))
