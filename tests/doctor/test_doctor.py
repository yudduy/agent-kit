import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from doctor import doctor  # noqa: E402

# Built at runtime so no credential-shaped string is committed.
STRIPE_LIVE = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
SERVICE_ROLE_JWT = ".".join(base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=")
                            for p in ({"alg": "HS256"}, {"role": "service_role", "iss": "supabase"})) + ".sig"
ANON_JWT = SERVICE_ROLE_JWT.replace(SERVICE_ROLE_JWT.split(".")[1], base64.urlsafe_b64encode(
    json.dumps({"role": "anon"}).encode()).decode().rstrip("="))
PROD_DB = "postgresql://postgres:hunter2@db.abcdefghijklmnopqrst.supabase.co:5432/postgres"
SHA = "0123456789abcdef0123456789abcdef01234567"


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def clean_repo(root):
    write(root, ".agent-kit/policy.yml", "version: 1\n")
    write(root, ".agent-kit/journeys/save.spec.ts", "")
    write(root, ".github/workflows/agent-verify.yml",
          f"jobs:\n  verify:\n    uses: yudduy/agent-kit/.github/workflows/verify.yml@{SHA}\n")
    write(root, ".github/workflows/ci.yml",
          "env:\n  DATABASE_URL: postgresql://postgres:pw@localhost:5432/app\n  REDIS_URL: redis://redis:6379\n")
    write(root, ".cursor/environment.json", json.dumps({"env": {"DATABASE_URL": "sqlite:///tmp/app.db",
                                                               "SUPABASE_ANON_KEY": ANON_JWT}}))
    write(root, ".cursor/skills/verify-app/SKILL.md", "")
    write(root, "docs/features/profile.md", "")
    write(root, ".github/CODEOWNERS", "* @yudduy\n")
    write(root, "AGENTS.md", "<!-- agent-kit:automation v1 seer:off cursor-sentry:off -->\n")


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = doctor.main([str(a) for a in argv])
    return code, out.getvalue()


class Doctor(unittest.TestCase):
    def test_clean_repo_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean_repo(tmp)
            self.assertEqual(run(tmp), (0, "doctor: ok\n"))

    def test_planted_prod_credentials_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean_repo(tmp)
            write(tmp, ".cursor/environment.json", json.dumps({"env": {
                "DATABASE_URL": PROD_DB, "SUPABASE_SERVICE_ROLE_KEY": "x", "VERCEL_ENV": "production",
                "REPLICA_URL": "postgresql://u@[2001:db8::2]:5432/app"}}))
            write(tmp, ".github/workflows/ci.yml",
                  f"env:\n  STRIPE_KEY: {STRIPE_LIVE}\n  DB: ${{{{ secrets.PROD_DATABASE_URL }}}}\n"
                  "  KEY: ${{ secrets['STRIPE_PRODUCTION_KEY'] }}\n"
                  "  VERCEL_ENV: production\n")
            write(tmp, "dump.env", f"SUPABASE_KEY={SERVICE_ROLE_JWT}\nNEXT_PUBLIC_URL=https://example.com\n")
            write(tmp, "names.txt", "SUPABASE_SERVICE_ROLE_KEY\nVERCEL_TOKEN\nGITHUB_TOKEN\n")
            code, out = run(tmp, "--env-file", Path(tmp) / "dump.env", "--env-names", Path(tmp) / "names.txt")
            self.assertEqual(code, 1)
            for expected in [
                ".cursor/environment.json: database URL with a remote host: db.abcdefghijklmnopqrst.supabase.co",
                ".cursor/environment.json: prod-looking secret name: SUPABASE_SERVICE_ROLE_KEY",
                ".cursor/environment.json: Vercel production binding",
                ".cursor/environment.json: database URL with a remote host: 2001:db8::2",
                ".github/workflows/ci.yml: Stripe live secret key",
                ".github/workflows/ci.yml: prod-looking secret name: PROD_DATABASE_URL",
                ".github/workflows/ci.yml: prod-looking secret name: STRIPE_PRODUCTION_KEY",
                ".github/workflows/ci.yml: Vercel production binding",
                "dump.env: Supabase service-role key",
                "names.txt: prod-looking secret name: SUPABASE_SERVICE_ROLE_KEY",
                "names.txt: prod-looking secret name: VERCEL_TOKEN",
            ]:
                self.assertIn(expected, out)
            self.assertNotIn("GITHUB_TOKEN", out)
            for secret in (STRIPE_LIVE, SERVICE_ROLE_JWT, "hunter2"):
                self.assertNotIn(secret, out)

    def test_missing_kit_files_and_unpinned_caller_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, ".github/workflows/agent-verify.yml",
                  'jobs:\n  verify:\n    uses: "yudduy/agent-kit/.github/workflows/verify.yml@v1"\n')
            code, out = run(tmp)
            self.assertEqual(code, 1)
            for expected in [".agent-kit/policy.yml: missing kit file", "CODEOWNERS: missing",
                             "kit is pinned to 'v1', not a full commit SHA",
                             "AGENTS.md: does not record Seer and the native Cursor Sentry trigger as off"]:
                self.assertIn(expected, out)


if __name__ == "__main__":
    unittest.main()
