"""Builders for gate test facts: one valid docs-only PR, which each test bends one way."""

import copy
import hashlib
import json

from gate.decide import KIT_VERIFY_WORKFLOW, Facts

REPO = "yudduy/demo"
KIT = "a" * 40
BASE = "b" * 40
HEAD = "c" * 40
POLICY_SHA = "d" * 40
JOURNEYS_SHA = "e" * 40
RUN_URI = f"https://github.com/{REPO}/actions/runs/7/attempts/1"
TRACE = b"trace bytes"

POLICY = f"""version: 1
kit_ref: {KIT}                     # pinned kit
approvers: [yudduy, alice]
app:
  build: "docker build -t app ."
  start: {{port: 3000, health: /healthz}}
  db: {{image: postgres:16, seed: db/seed.sql}}
floor_extra:
  migrations: {{paths: ["db/migrations/**"]}}
  governance: {{paths: ["evals/**"]}}
auto_merge: [docs, kit-bump]
required_checks: [{{name: test, app: github-actions}}]
journeys:
  - id: save-profile
    areas: ["src/profile/**"]
    spec: .agent-kit/journeys/save-profile.spec.ts
    data_assertions:
      - {{id: name-persisted, sql: "select name from profiles where id = 'seed-user-1'", expect: [{{name: Ada}}]}}
"""


def change(filename, status="modified", patch="@@ -1 +1 @@\n-old\n+new", **extra):
    entry = {"filename": filename, "status": status, **extra}
    if patch is not None:
        entry["patch"] = patch
    return entry


def evidence_doc():
    return {
        "schema": 1, "repo": REPO, "pr": 12, "head_sha": HEAD, "base_sha": BASE, "kit_ref": KIT,
        "policy_sha": POLICY_SHA, "journeys_sha": JOURNEYS_SHA,
        "run": {"id": 7, "attempt": 1, "workflow_ref": f"{KIT_VERIFY_WORKFLOW}@{KIT}"},
        "env": {"runner_image": "ubuntu-24.04/20261101", "app_image_digest": "sha256:1", "db_image_digest": "sha256:2", "seed_hash": "sha256:3"},
        "scenarios": [{
            "id": "save-profile", "source": "base", "ui_result": "pass",
            "data_assertions": [{"id": "name-persisted", "sql_sha256": "f" * 64, "expected": [{"name": "Ada"}], "observed": [{"name": "Ada"}], "pass": True}],
            "result": "pass", "trace": "traces/save-profile.zip",
        }],
        "regression": [],
        "artifacts": [{"name": "traces/save-profile.zip", "sha256": hashlib.sha256(TRACE).hexdigest()}],
        "result": "pass",
    }


def attestation_for(data):
    return {"subject_sha256": hashlib.sha256(data).hexdigest(), "signer_workflow": KIT_VERIFY_WORKFLOW,
            "signer_digest": KIT, "run_invocation_uri": RUN_URI}


def review(login, user_type="User", sha=HEAD, state="APPROVED", at="2026-11-10T10:00:00Z"):
    return {"user": {"login": login, "type": user_type}, "state": state, "commit_id": sha, "submitted_at": at}


def facts(files=None, edit=None, tamper=None, **overrides):
    """Valid facts for PR #12 by the agent bot. `edit` changes the evidence before attesting; `tamper` after."""
    doc = evidence_doc()
    if edit:
        edit(doc)
    data = json.dumps(doc).encode()
    attestation = attestation_for(data)
    if tamper:
        tampered = copy.deepcopy(doc)
        tamper(tampered)
        data = json.dumps(tampered).encode()
    values = dict(
        repo=REPO,
        pr={"number": 12, "draft": False, "user": {"login": "agent-bot[bot]", "type": "Bot"},
            "head": {"sha": HEAD, "repo": {"full_name": REPO}}, "base": {"ref": "main", "repo": {"default_branch": "main"}}},
        base_sha=BASE,
        behind_by=0,
        files=files if files is not None else [change("docs/guide.md")],
        commits=[{"author": {"login": "agent-bot[bot]"}, "committer": {"login": "web-flow"}}],
        reviews=[],
        checks={"test": {"github-actions": "success"}},
        kit_pins={".github/workflows/verify.yml": [KIT]},
        policy_text=POLICY,
        policy_sha=POLICY_SHA,
        journeys_sha=JOURNEYS_SHA,
        evidence=data,
        artifact_files={"evidence.json": hashlib.sha256(data).hexdigest(), "traces/save-profile.zip": hashlib.sha256(TRACE).hexdigest()},
        attestation=attestation,
    )
    values.update(overrides)
    return Facts(**values)
