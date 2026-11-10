"""GitHub REST calls the dispatcher and Mac mini helper need: issues, comments, labels, one file read."""
import base64
import re
import urllib.parse

from dispatcher import http

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token, send=http.send):
        self._headers = {"Authorization": "Bearer " + token, "X-GitHub-Api-Version": "2022-11-28"}
        self._send = send

    def _call(self, method, path, body=None, ok=(200, 201, 204)):
        status, headers, data = self._send(method, path if path.startswith("http") else API + path, self._headers, body)
        if status not in ok:
            raise GitHubError("%s %s returned %s: %s" % (method, path, status, (data or {}).get("message")))
        return status, headers, data

    def _pages(self, path):
        url = API + path
        while url:
            _, headers, data = self._call("GET", url)
            yield from data
            match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link") or headers.get("link") or "")
            url = match.group(1) if match else None

    def whoami(self):
        return self._call("GET", "/user")[2]["login"]

    def issues(self, repo, label=None, state="open"):
        query = urllib.parse.urlencode({**({"labels": label} if label else {}), "state": state, "per_page": 100})
        return [i for i in self._pages("/repos/%s/issues?%s" % (repo, query)) if "pull_request" not in i]

    def create_issue(self, repo, title, body, labels):
        return self._call("POST", "/repos/%s/issues" % repo, {"title": title, "body": body, "labels": labels})[2]

    def update_issue(self, repo, number, **fields):
        return self._call("PATCH", "/repos/%s/issues/%d" % (repo, number), fields)[2]

    def add_labels(self, repo, number, labels):
        self._call("POST", "/repos/%s/issues/%d/labels" % (repo, number), {"labels": labels})

    def remove_label(self, repo, number, label):
        self._call("DELETE", "/repos/%s/issues/%d/labels/%s" % (repo, number, urllib.parse.quote(label)), ok=(200, 204, 404))

    def assign(self, repo, number, logins):
        self._call("POST", "/repos/%s/issues/%d/assignees" % (repo, number), {"assignees": logins})

    def comments(self, repo, number):
        return list(self._pages("/repos/%s/issues/%d/comments?per_page=100" % (repo, number)))

    def comment(self, repo, number, body):
        return self._call("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body": body})[2]

    def edit_comment(self, repo, comment_id, body):
        return self._call("PATCH", "/repos/%s/issues/comments/%d" % (repo, comment_id), {"body": body})[2]

    def events(self, repo, number):
        return list(self._pages("/repos/%s/issues/%d/events?per_page=100" % (repo, number)))

    def file(self, repo, path):
        """Text of `path` on the default branch, or None when it does not exist."""
        status, _, data = self._call("GET", "/repos/%s/contents/%s" % (repo, path), ok=(200, 404))
        return base64.b64decode(data["content"]).decode() if status == 200 else None
