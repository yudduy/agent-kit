"""agent-kit doctor: check a repo checkout for prod credentials (INV-7) and missing kit files.

Scans `.cursor/environment.json`, every `.github/workflows/*.yml`, and any env dumps
(`--env-file`, KEY=VALUE lines) or secret-name lists (`--env-names`, one name per
line) you pass. Never prints a secret value. No network. Exits 1 on any finding.
"""
import argparse
import base64
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

KIT_FILES = [".agent-kit/policy.yml", ".agent-kit/journeys", ".github/workflows/agent-verify.yml",
             ".cursor/environment.json", "docs/features"]
CODEOWNERS = ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]
CALLER = re.compile(r"""uses:\s*["']?yudduy/agent-kit/\.github/workflows/verify\.yml@([^\s"']+)""")
AUTOMATION_MARKER = "<!-- agent-kit:automation v1 seer:off cursor-sentry:off -->"

SECRET_VALUES = [
    (re.compile(r"\b[sr]k_live_[0-9A-Za-z]{8,}"), "Stripe live secret key"),
    (re.compile(r"\bsb_secret_[0-9A-Za-z_-]{8,}"), "Supabase secret key"),
    (re.compile(r"https://[a-z0-9]{20}\.supabase\.co\b"), "hosted Supabase project URL"),
    (re.compile(r"""\bVERCEL_ENV["']?\s*[=:]\s*["']?production\b"""), "Vercel production binding"),
]
JWT = re.compile(r"\beyJ[0-9A-Za-z_-]+\.(eyJ[0-9A-Za-z_-]+)\.[0-9A-Za-z_-]+")
DB_URL = re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|rediss?)://[^\s'\"]+")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
PROD_NAME = re.compile(r"(^|_)(PROD|PRODUCTION)(_|$)|SERVICE_ROLE|^VERCEL_TOKEN$|^SUPABASE_ACCESS_TOKEN$")
NAME_IN_TEXT = re.compile(r"""\b(?:secrets|vars|env)(?:\.(\w+)|\[["'](\w+)["']\])|\b([A-Z][A-Z0-9_]*)["']?\s*[:=]""")


def _redact(value):
    return value[:6] + "…"


def _is_service_role_jwt(payload):
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return False
    return isinstance(claims, dict) and claims.get("role") == "service_role"


def scan_text(text):
    """Findings for prod-looking credentials in a blob of text."""
    found = []
    for pattern, label in SECRET_VALUES:
        found += [f"{label}: {_redact(m.group(0))}" for m in pattern.finditer(text)]
    found += [f"Supabase service-role key: {_redact(m.group(0))}" for m in JWT.finditer(text)
              if _is_service_role_jwt(m.group(1))]
    for m in DB_URL.finditer(text):
        try:
            host = urlsplit(m.group(0)).hostname or ""
        except ValueError:
            host = "?"
        if host not in LOCAL_HOSTS and ("." in host or ":" in host):
            found.append(f"database URL with a remote host: {host}")
    return found + scan_names(names_in(text))


def scan_names(names):
    return [f"prod-looking secret name: {n}" for n in sorted(set(names)) if PROD_NAME.search(n.upper())]


def names_in(text):
    return ["".join(groups) for groups in NAME_IN_TEXT.findall(text)]


def check_repo(repo):
    findings = []
    for rel in KIT_FILES:
        if not (repo / rel).exists():
            findings.append(f"{rel}: missing kit file")
    if not any((repo / p).is_file() for p in CODEOWNERS):
        findings.append("CODEOWNERS: missing")
    if not list(repo.glob(".cursor/skills/verify-*")):
        findings.append(".cursor/skills/verify-<app>/: missing")
    caller = repo / ".github/workflows/agent-verify.yml"
    if caller.is_file():
        refs = CALLER.findall(caller.read_text())
        if not refs:
            findings.append(f"{caller.relative_to(repo)}: does not call yudduy/agent-kit verify.yml")
        for ref in refs:
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                findings.append(f"{caller.relative_to(repo)}: kit is pinned to {ref!r}, not a full commit SHA")
    agents = repo / "AGENTS.md"
    if not (agents.is_file() and AUTOMATION_MARKER in agents.read_text()):
        findings.append("AGENTS.md: does not record Seer and the native Cursor Sentry trigger as off "
                        f"(add `{AUTOMATION_MARKER}`)")
    sources = [repo / ".cursor/environment.json", *sorted(repo.glob(".github/workflows/*.y*ml"))]
    for path in sources:
        if path.is_file():
            text = path.read_text(errors="replace")
            rel = path.relative_to(repo)
            findings += [f"{rel}: {f}" for f in scan_text(text)]
    return findings


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("repo", nargs="?", default=".", help="repo checkout to scan")
    p.add_argument("--env-file", action="append", default=[], help="KEY=VALUE env dump to scan")
    p.add_argument("--env-names", action="append", default=[], help="file of secret names, one per line")
    args = p.parse_args(argv)
    findings = check_repo(Path(args.repo).resolve())
    for path in args.env_file:
        text = Path(path).read_text(errors="replace")
        findings += [f"{path}: {f}" for f in scan_text(text)]
    for path in args.env_names:
        names = [n.strip() for n in Path(path).read_text().splitlines() if n.strip()]
        findings += [f"{path}: {f}" for f in scan_names(names)]
    for f in findings:
        print(f"doctor: {f}")
    print(f"doctor: {len(findings)} finding(s)" if findings else "doctor: ok")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
