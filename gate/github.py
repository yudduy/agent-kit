"""GitHub access for the gate: a live client (urllib + App installation tokens) and a fixture client for --dry-run.

Both expose get, pages, post, put, artifact_files and verify_attestation. Neither ever clones or runs PR code (INV-2).
"""

import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from gate.decide import KIT_VERIFY_WORKFLOW

API = "https://api.github.com"


class NotFound(Exception):
    pass


class GitHub:
    def __init__(self, token):
        self.token = token

    def _request(self, method, path, body=None):
        request = urllib.request.Request(
            path if path.startswith("https://") else API + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "agent-gate",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise NotFound(f"{method} {path}") from None
            raise RuntimeError(f"{method} {path}: HTTP {error.code} {error.read()[:300]!r}") from None
        return json.loads(data) if data else None

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body):
        return self._request("POST", path, body)

    def put(self, path, body):
        return self._request("PUT", path, body)

    def pages(self, path, key=None):
        items, page = [], 1
        while True:
            data = self.get(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            batch = data[key] if key else data
            items += batch
            if len(batch) < 100:
                return items
            page += 1

    def artifact_files(self, repo, artifact_id, max_bytes):
        """Download an Actions artifact zip; return {member name: bytes}."""
        request = urllib.request.Request(
            f"{API}/repos/{repo}/actions/artifacts/{artifact_id}/zip",
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "agent-gate"},
        )
        # The API answers with a redirect to signed blob storage; follow it without our token.
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(request, timeout=60)
            raise RuntimeError(f"artifact {artifact_id}: expected a redirect")
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                raise RuntimeError(f"artifact {artifact_id}: HTTP {error.code}") from None
            location = error.headers["Location"]
        with urllib.request.urlopen(location, timeout=120) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read(max_bytes + 1)))
        if sum(info.file_size for info in archive.infolist()) > max_bytes:
            raise RuntimeError(f"artifact {artifact_id}: unpacks to more than {max_bytes} bytes")
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}

    def verify_attestation(self, repo, data, kit_ref):
        """Verify the sigstore attestation of `data` with the gh CLI; return its facts, or None if it fails."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_bytes(data)
            result = subprocess.run(
                ["gh", "attestation", "verify", str(path), "--repo", repo,
                 "--signer-workflow", KIT_VERIFY_WORKFLOW, "--signer-digest", kit_ref,
                 "--deny-self-hosted-runners", "--format", "json"],
                capture_output=True, text=True, timeout=120, env={**os.environ, "GH_TOKEN": self.token},
            )
        if result.returncode != 0:
            return None
        certificate = json.loads(result.stdout)[0]["verificationResult"]["signature"]["certificate"]
        return {
            "subject_sha256": hashlib.sha256(data).hexdigest(),
            "signer_workflow": certificate["buildSignerURI"].removeprefix("https://github.com/").split("@", 1)[0],
            "signer_digest": certificate["buildSignerDigest"],
            "run_invocation_uri": certificate["runInvocationURI"],
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def app_jwt(app_id, private_key_pem, now=None):
    """RS256 JWT for the App. The standard library has no RSA, so `openssl` (present on GitHub runners) signs it."""
    now = int(now or time.time())

    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    claims = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    signing_input = b64(b'{"alg":"RS256","typ":"JWT"}') + b"." + b64(json.dumps(claims).encode())
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as key:  # created 0600
        key.write(private_key_pem)
        key.flush()
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key.name], input=signing_input, capture_output=True, check=True
        ).stdout
    return (signing_input + b"." + b64(signature)).decode()


def installation_client(app_id, private_key_pem, repo):
    """A client holding an installation token scoped to this one repository."""
    app = GitHub(app_jwt(app_id, private_key_pem))
    installation = app.get(f"/repos/{repo}/installation")
    token = app.post(f"/app/installations/{installation['id']}/access_tokens", {"repositories": [repo.split("/", 1)[1]]})
    return GitHub(token["token"])


class Fixture:
    """Reads API responses from a fixture directory and records writes instead of sending them.

    Layout: api.json maps "<GET path>" to its JSON response; artifacts/<id>/ holds artifact files;
    attestations.json maps an evidence sha256 to {signer_workflow, signer_digest, run_invocation_uri}.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.api = json.loads((self.root / "api.json").read_text())
        attestations = self.root / "attestations.json"
        self.attestations = json.loads(attestations.read_text()) if attestations.exists() else {}
        self.reads, self.writes = [], []

    def get(self, path):
        self.reads.append(path)
        if path not in self.api:
            raise NotFound(f"GET {path}")
        return self.api[path]

    def pages(self, path, key=None):
        data = self.get(path)
        return data[key] if key else data

    def post(self, path, body):
        self.writes.append(("POST", path, body))
        return {}

    def put(self, path, body):
        self.writes.append(("PUT", path, body))
        return {}

    def artifact_files(self, repo, artifact_id, max_bytes):
        folder = self.root / "artifacts" / str(artifact_id)
        return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}

    def verify_attestation(self, repo, data, kit_ref):
        digest = hashlib.sha256(data).hexdigest()
        found = self.attestations.get(digest)
        if found is None or found["signer_digest"] != kit_ref:
            return None
        return {"subject_sha256": digest, **found}
