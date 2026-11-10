import base64
import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from gate import main as gate_main
from gate.decide import summary
from gate.github import Fixture, app_jwt
from tests.gate.helpers import BASE, HEAD, REPO

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tick"
CONTROL = "yudduy/agent-control"
APP_ID = 4242


def tick(fixture, paused=False):
    errors = gate_main.run(lambda repo: fixture, fixture, CONTROL, paused, APP_ID, "2026-11-10")
    return errors, fixture.writes


def kinds(writes):
    return [(method, path) for method, path, _ in writes]


class TickTest(unittest.TestCase):
    def test_docs_pr_merges_and_governance_pr_does_not(self):
        errors, writes = tick(Fixture(FIXTURE))
        self.assertEqual(errors, 0)
        checks = {body["head_sha"]: body["conclusion"] for method, path, body in writes if path.endswith("/check-runs")}
        self.assertEqual(checks, {HEAD: "success", "9" * 40: "failure"})
        merges = [(path, body) for method, path, body in writes if path.endswith("/merge")]
        self.assertEqual(merges, [(f"/repos/{REPO}/pulls/12/merge", {"sha": HEAD})])
        log = [body for method, path, body in writes if path == f"/repos/{CONTROL}/contents/decisions/2026-11-10.jsonl"]
        self.assertEqual([body["branch"] for body in log], ["decisions-log", "decisions-log"])
        self.assertEqual([json.loads(base64.b64decode(body["content"]))["pr"] for body in log], [12, 13])
        self.assertEqual(kinds(writes)[:3], [("PUT", f"/repos/{CONTROL}/contents/decisions/2026-11-10.jsonl"),
                                             ("POST", f"/repos/{REPO}/check-runs"), ("PUT", f"/repos/{REPO}/pulls/12/merge")])

    def test_paused_posts_verdicts_but_never_merges(self):
        errors, writes = tick(Fixture(FIXTURE), paused=True)
        self.assertEqual(errors, 0)
        self.assertFalse([w for w in writes if w[1].endswith("/merge")])
        conclusions = [body["conclusion"] for _, path, body in writes if path.endswith("/check-runs")]
        self.assertEqual(conclusions, ["failure", "failure"])

    def test_policy_and_journeys_are_read_from_base_never_head_inv3(self):
        fixture = Fixture(FIXTURE)
        tick(fixture)
        contents = [path for path in fixture.reads if f"/repos/{REPO}/contents/" in path]
        self.assertIn(f"/repos/{REPO}/contents/.github/workflows/verify.yml?ref={BASE}", contents)
        self.assertTrue(all(path.endswith(f"?ref={BASE}") for path in contents), contents)

    def test_unchanged_decision_is_not_reposted_but_merge_is_retried(self):
        fixture = Fixture(FIXTURE)
        _, first = tick(fixture)
        for method, path, body in first:
            if path.endswith("/check-runs"):
                runs = fixture.api[f"/repos/{REPO}/commits/{body['head_sha']}/check-runs"]["check_runs"]
                runs.append({"id": 999, "name": "agent-gate", "status": "completed", "conclusion": body["conclusion"],
                             "app": {"id": APP_ID}, "output": {"summary": body["output"]["summary"]}})
        fixture.writes.clear()
        self.assertEqual(tick(fixture)[1], [("PUT", f"/repos/{REPO}/pulls/12/merge", {"sha": HEAD})])

    def test_human_approved_pr_is_merged_by_the_gate_d2(self):
        fixture = Fixture(FIXTURE)
        fixture.api[f"/repos/{REPO}/pulls/13/reviews"] = [
            {"user": {"login": "alice", "type": "User"}, "state": "APPROVED", "commit_id": "9" * 40, "submitted_at": "2026-11-10T10:00:00Z"}]
        _, writes = tick(fixture)
        self.assertIn(("PUT", f"/repos/{REPO}/pulls/13/merge", {"sha": "9" * 40}), writes)

    def test_required_check_from_another_app_blocks_merge(self):
        fixture = Fixture(FIXTURE)
        fixture.api[f"/repos/{REPO}/commits/{HEAD}/check-runs"]["check_runs"][0]["app"] = {"id": 1, "slug": "evil-app"}
        _, writes = tick(fixture)
        self.assertNotIn(("PUT", f"/repos/{REPO}/pulls/12/merge"), kinds(writes))

    def test_base_kit_pin_mismatch_blocks_merge_d1(self):
        fixture = Fixture(FIXTURE)
        path = f"/repos/{REPO}/contents/.github/workflows/verify.yml?ref={BASE}"
        text = base64.b64decode(fixture.api[path]["content"]).decode().replace("a" * 40, "9" * 40)
        fixture.api[path]["content"] = base64.b64encode(text.encode()).decode()
        _, writes = tick(fixture)
        self.assertNotIn(("PUT", f"/repos/{REPO}/pulls/12/merge"), kinds(writes))

    def test_unreachable_repo_does_not_stop_the_others(self):
        fixture = Fixture(FIXTURE)
        config = {"v": 1, "repos": ["gone/repo", REPO]}
        fixture.api[f"/repos/{CONTROL}/contents/agent-kit/repos.json?ref=main"]["content"] = base64.b64encode(json.dumps(config).encode()).decode()
        with contextlib.redirect_stderr(io.StringIO()):
            errors, writes = tick(fixture)
        self.assertEqual(errors, 1)
        self.assertIn(("PUT", f"/repos/{REPO}/pulls/12/merge"), kinds(writes))

    def test_no_check_or_merge_without_a_durable_log_line(self):
        fixture = Fixture(FIXTURE)
        def refuse(path, body):
            raise RuntimeError("HTTP 403")
        fixture.put = refuse
        with contextlib.redirect_stderr(io.StringIO()):
            errors, writes = tick(fixture)
        self.assertEqual((errors, writes), (2, []))

    def test_unreadable_evidence_artifact_fails_the_check(self):
        fixture = Fixture(FIXTURE)
        def corrupt(repo, artifact_id, max_bytes):
            raise zipfile.BadZipFile("File is not a zip file")
        fixture.artifact_files = corrupt
        _, writes = tick(fixture)
        body = next(b for _, p, b in writes if p.endswith("/check-runs") and b["head_sha"] == HEAD)
        self.assertEqual(body["conclusion"], "failure")
        self.assertIn('"evidence": "missing"', body["output"]["summary"])

    def test_oversized_evidence_artifact_counts_as_missing(self):
        fixture = Fixture(FIXTURE)
        fixture.api[f"/repos/{REPO}/actions/runs/7/artifacts?name=agent-kit-evidence"]["artifacts"][0]["size_in_bytes"] = 10**9
        _, writes = tick(fixture)
        body = next(b for _, p, b in writes if p.endswith("/check-runs") and b["head_sha"] == HEAD)
        self.assertIn('"evidence": "missing"', body["output"]["summary"])

    def test_agent_gate_check_from_another_app_is_ignored(self):
        fixture = Fixture(FIXTURE)
        runs = fixture.api[f"/repos/{REPO}/commits/{HEAD}/check-runs"]["check_runs"]
        fake = summary({"conclusion": "success", "reasons": [], "class": "docs", "shadow": "would_merge"})[1]
        runs.append({"id": 998, "name": "agent-gate", "status": "completed", "conclusion": "success", "app": {"id": 1}, "output": {"summary": fake}})
        self.assertIn(("POST", f"/repos/{REPO}/check-runs"), kinds(tick(fixture)[1]))

    def test_one_failing_pr_does_not_stop_the_others(self):
        fixture = Fixture(FIXTURE)
        del fixture.api[f"/repos/{REPO}/compare/{BASE}..." + "9" * 40]
        with contextlib.redirect_stderr(io.StringIO()):
            errors, writes = tick(fixture)
        self.assertEqual(errors, 1)
        self.assertEqual([body["head_sha"] for _, path, body in writes if path.endswith("/check-runs")], [HEAD])

    def test_hand_edited_evidence_artifact_fails_inv6(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["cp", "-R", f"{FIXTURE}/.", tmp], check=True)
            path = Path(tmp) / "artifacts" / "120" / "evidence.json"
            path.write_text(path.read_text().replace('"result": "pass"\n', '"result": "pass" \n'))
            _, writes = tick(Fixture(tmp))
        bodies = [body for _, p, body in writes if p.endswith("/check-runs") and body["head_sha"] == HEAD]
        self.assertEqual(bodies[0]["conclusion"], "failure")
        self.assertIn("attestation", bodies[0]["output"]["summary"])
        self.assertFalse([w for w in writes if w[1].endswith("/merge")])

    def test_cli_dry_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gate_main.main(["--dry-run", str(FIXTURE)]), 0)
        self.assertIn(f'"path": "/repos/{REPO}/pulls/12/merge"', out.getvalue())


class DeploymentTest(unittest.TestCase):
    def test_workflow_checks_out_only_pinned_kit_inv2(self):
        text = (ROOT / "templates" / "agent-control" / ".github" / "workflows" / "gate.yml").read_text()
        self.assertNotRegex(text, r"\bpull_request")
        self.assertEqual(re.findall(r"uses: (\S+)", text), ["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"])
        self.assertIn("repository: yudduy/agent-kit\n          ref: ${{ env.AGENT_KIT_REF }}", text)
        self.assertRegex(text, r'AGENT_KIT_REF: "[0-9a-f]{40}"')
        self.assertIn("environment: merge-gate", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("*/5 * * * *", text)

    def test_gate_code_never_fetches_or_runs_pr_code_inv2(self):
        for path in (ROOT / "gate").glob("*.py"):
            text = path.read_text()
            self.assertNotRegex(text, r"\bgit\b.*(clone|checkout|fetch)|/tarball|/zipball", path.name)
            self.assertNotRegex(text, r"os\.system|os\.exec|shell=True", path.name)
            for command in re.findall(r'subprocess\.\w+\(\s*\[\s*"([\w-]+)"', text):
                self.assertIn(command, ("gh", "openssl"), path.name)
        self.assertEqual(len(re.findall(r"subprocess\.run\(", (ROOT / "gate" / "github.py").read_text())), 2)

    def test_ruleset_binds_check_to_app_with_no_bypass_inv1(self):
        ruleset = json.loads((ROOT / "gate" / "ruleset.json").read_text())
        self.assertEqual(ruleset["bypass_actors"], [])
        (rule,) = ruleset["rules"]
        self.assertTrue(rule["parameters"]["strict_required_status_checks_policy"])
        self.assertEqual([c["context"] for c in rule["parameters"]["required_status_checks"]], ["agent-gate"])
        self.assertIn("integration_id", rule["parameters"]["required_status_checks"][0])

    def test_app_jwt_is_rs256_signed_by_openssl(self):
        with tempfile.TemporaryDirectory() as tmp:
            key, pub, sig, data = (Path(tmp) / n for n in ("k.pem", "p.pem", "s.bin", "d.txt"))
            subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
            subprocess.run(["openssl", "rsa", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
            token = app_jwt(123, key.read_text(), now=1_700_000_000)
            header, claims, signature = token.split(".")
            pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
            self.assertEqual(json.loads(pad(header)), {"alg": "RS256", "typ": "JWT"})
            self.assertEqual(json.loads(pad(claims)), {"iat": 1_699_999_940, "exp": 1_700_000_540, "iss": "123"})
            data.write_text(f"{header}.{claims}")
            sig.write_bytes(pad(signature))
            verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sig, data], capture_output=True, text=True)
            self.assertIn("Verified OK", verified.stdout)


if __name__ == "__main__":
    unittest.main()
