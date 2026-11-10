"""The one HTTP call every client uses; tests replace `send` with a fake."""
import json
import urllib.error
import urllib.request


class Unreachable(Exception):
    """The request may or may not have reached the server."""


def send(method, url, headers=None, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json", **(headers or {})})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), _json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), _json(error.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        raise Unreachable(str(error)) from error


def _json(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None
