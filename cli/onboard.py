"""`agent-kit init|journeys|sync`: onboard a repo with one draft PR, start its journeys run, bump its kit SHA."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TEMPLATES = KIT / "templates" / "repo"
BRANCH = "agent-kit/onboard"
PSTACK = ("https://github.com/backnotprop/pstack", "4161d4ef5051574e825ffe70e2f43a2e49e4b3db")
# Template files merged line by line into whichever existing copy the repo has, instead of being kept or overwritten.
APPEND = {".github/CODEOWNERS": (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"), "AGENTS.md": ("AGENTS.md",)}
REGISTER = "Register: add %s to `agent-kit/repos.json` in agent-control so the gate, dispatcher and harvest pick it up."
USES = re.compile(r"""(uses:\s*['"]?yudduy/agent-kit/[^@\s'"]+@)([0-9a-f]{40})""")
TITLE = "Add agent kit onboarding"

JOURNEYS_PROMPT = """You are onboarding {repo} to agent-kit on branch {branch}. Commit to that branch only; do not open a new PR.
1. Clone pstack at its pinned commit: `git clone {pstack} /tmp/pstack && git -C /tmp/pstack checkout {pstack_sha}`.
   Follow /tmp/pstack/skills/create-verification-skill/SKILL.md to write `.cursor/skills/verify-<app>/` and `docs/features/`.
2. Propose the 3 to 5 journeys a user cares most about. For each, add `.agent-kit/journeys/<id>.spec.ts`, a Playwright spec
   that drives the real app, and list it under `journeys:` in `.agent-kit/policy.yml` with its `areas` globs and at least one
   `data_assertions` entry: SQL run straight against the database seeded from `.agent-kit/seed/seed.sql`, with the rows a
   correct save leaves behind. A journey that only checks the screen is not done.
3. Fill `app:` in `.agent-kit/policy.yml` (build, start port and health path, db image and seed) and `.cursor/environment.json`
   so the app builds and starts from a fresh machine. If the repo has no root `Dockerfile`, add one: the kit's verify
   workflow runs `docker build` on it. Use synthetic seed data only; never a production URL or key.
4. Do not change `kit_ref`, `approvers`, `auto_merge`, `caps` or the workflow files.
Finish with a short list: each journey, the stored data it checks, and anything you could not run."""


def run(*args, cwd=None, capture=True):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=capture)
    if result.returncode:
        sys.exit("Command failed: %s\n%s" % (" ".join(args), (result.stderr or "").strip()))
    return (result.stdout or "").strip()


def kit_ref(ref=None, require_pushed=True):
    sha = ref or run("git", "-C", str(KIT), "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sys.exit("The kit ref must be a full 40-character commit SHA, got %r." % sha)
    if require_pushed and not ref:
        on_main = subprocess.run(["git", "-C", str(KIT), "merge-base", "--is-ancestor", sha, "origin/main"]).returncode == 0
        if not on_main:
            sys.exit("Kit commit %s is not on origin/main, so repos cannot call it. Update the kit checkout or pass --ref." % sha[:12])
    return sha


def render(repo, ref, templates=TEMPLATES):
    """{relative path: text} for every template file, with the kit SHA and repo filled in."""
    out = {}
    for path in sorted(p for p in templates.rglob("*") if p.is_file()):
        text = path.read_text().replace("{{KIT_REF}}", ref).replace("{{REPO}}", repo).replace("{{APP}}", repo.split("/")[-1])
        out[str(path.relative_to(templates))] = text
    return out


def plan(checkout, files, force=False):
    """(path, action, text) writes for a checkout: APPEND files gain their missing lines, other existing files are kept."""
    if (checkout / ".agent-kit").exists() and not force:
        sys.exit("This repo already has .agent-kit/. Use `agent-kit sync` to bump the kit, or pass --force to overwrite.")
    writes = []
    for rel, text in files.items():
        if rel in APPEND:
            target = next((p for p in APPEND[rel] if (checkout / p).exists()), rel)
            existing = (checkout / target).read_text() if (checkout / target).exists() else ""
            missing = [l for l in text.splitlines() if l.strip() and l not in existing.splitlines()]
            if missing:
                writes.append((target, "append" if existing else "create", existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(missing) + "\n"))
        elif (checkout / rel).exists() and not (force and rel.startswith(".agent-kit/")):
            writes.append((rel, "keep", None))
        else:
            writes.append((rel, "overwrite" if (checkout / rel).exists() else "create", text))
    return writes


def pr_body(repo, ref, writes):
    lines = "\n".join("- `%s` (%s)" % (p, a) for p, a, _ in writes if a != "keep")
    return """## Summary
**Before:** {repo} has no agent kit, so agent PRs cannot be verified or gated.
**After:** {repo} calls the kit's verify workflow at a pinned commit and has a policy file, owners for governance paths and a feature map.
**Why:** production errors can then be fixed by cloud agents while only the gate can merge.
**Users see:** Nothing changes until the journeys are reviewed and this PR is merged.

**Details**
Kit commit `{ref}`. A Cursor run adds 3 to 5 journeys with stored-data checks and the verify skill to this branch.
{lines}

## Review
- Read each journey under `.agent-kit/journeys/` and its `data_assertions` in `.agent-kit/policy.yml`: does it check what a user cares about?
- Check `app:` builds and starts the app, and that the seed holds only synthetic data.
""".format(repo=repo, ref=ref, lines=lines)


def start_journeys(repo, branch=BRANCH, client=None):
    key = os.environ.get("CURSOR_API_KEY")
    if client is None and not key:
        print("No CURSOR_API_KEY is set, so no Cursor run was started.\nNext step: export CURSOR_API_KEY=<onboarding key> && agent-kit journeys %s" % repo)
        return None
    if client is None:
        from dispatcher import cursor
        client = cursor.Client(key)
    prompt = JOURNEYS_PROMPT.format(repo=repo, branch=branch, pstack=PSTACK[0], pstack_sha=PSTACK[1])
    agent = client.create(str(uuid.uuid4()), "agent-kit onboarding %s" % repo, prompt, repo, branch, work_on_branch=True, auto_pr=False)
    print("Cursor run started: %s" % agent.get("url", agent["id"]))
    return agent


def clone(repo):
    tmp = Path(tempfile.mkdtemp(prefix="agent-kit-"))
    run("gh", "repo", "clone", repo, str(tmp / "repo"), "--", "--depth", "1")
    return tmp, tmp / "repo"


def init(repo, ref=None, dry_run=False, force=False):
    sha = kit_ref(ref, require_pushed=not dry_run)
    files = render(repo, sha)
    if dry_run:
        print("Dry run for %s at kit commit %s. Nothing is cloned, pushed or opened.\n\nFiles:" % (repo, sha))
        for rel in files:
            print("- %s%s" % (rel, " (missing lines appended if the repo already has one)" if rel in APPEND else ""))
        print("\nBranch: %s\nDraft PR: %s\n\n%s\nThen: one Cursor run on %s to propose 3-5 journeys (and add a Dockerfile if missing).\n%s"
              % (BRANCH, TITLE, pr_body(repo, sha, [(r, "create", t) for r, t in files.items()]), BRANCH, REGISTER % repo))
        return 0
    tmp, checkout = clone(repo)
    try:
        writes = plan(checkout, files, force)
        if not (checkout / "Dockerfile").exists():
            print("No Dockerfile at the repo root: the onboarding run will add one, because the verify workflow builds with `docker build`.")
        base = run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=checkout)
        run("git", "checkout", "-b", BRANCH, cwd=checkout)
        for rel, action, text in writes:
            if action != "keep":
                (checkout / rel).parent.mkdir(parents=True, exist_ok=True)
                (checkout / rel).write_text(text)
        run("git", "add", "-A", cwd=checkout)
        run("git", "commit", "-m", TITLE, cwd=checkout)
        run("git", "push", "-u", "origin", BRANCH, cwd=checkout)
        body = tmp / "body.md"
        body.write_text(pr_body(repo, sha, writes))
        print(run("gh", "pr", "create", "-R", repo, "--draft", "--base", base, "--head", BRANCH, "--title", TITLE, "--body-file", str(body)))
    finally:
        shutil.rmtree(tmp)
    start_journeys(repo)
    print(REGISTER % repo)
    print("Turn off Sentry's Seer handoff and Cursor's native Sentry trigger: AGENTS.md now declares both off, and `agent-kit doctor` checks it.")
    return 0


def bump(checkout, new):
    """Move every kit pin to `new`: `kit_ref`, every `uses: yudduy/agent-kit/...@<sha>` workflow line, and any other
    copy of the old `kit_ref` in a file `init` templates (such as the learning check's checkout `ref:`).
    Returns changed paths."""
    policy = checkout / ".agent-kit" / "policy.yml"
    found = re.search(r"""^kit_ref:\s*['"]?([0-9a-f]{40})['"]?\s*(#.*)?$""", policy.read_text(), re.M) if policy.exists() else None
    old_ref = found.group(1) if found else None
    workflows = {str(p.relative_to(checkout)) for p in (checkout / ".github" / "workflows").glob("*.y*ml")}
    templated, changed = set(render("", new)), []
    for rel in sorted(workflows | templated):
        path = checkout / rel
        if not path.exists():
            continue
        old = path.read_text()
        text = USES.sub(lambda mt: mt.group(1) + new, old)
        if old_ref and rel in templated:
            text = text.replace(old_ref, new)
        if text != old:
            path.write_text(text)
            changed.append(rel)
    return changed


def sync(repo, ref=None):
    sha = kit_ref(ref)
    tmp, checkout = clone(repo)
    try:
        branch = "agent-kit/bump-" + sha[:12]
        changed = bump(checkout, sha)
        if not changed:
            print("%s already pins kit commit %s." % (repo, sha[:12]))
            return 0
        base = run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=checkout)
        run("git", "checkout", "-b", branch, cwd=checkout)
        run("git", "commit", "-am", "Bump agent kit to %s" % sha[:12], cwd=checkout)
        run("git", "push", "-u", "origin", branch, cwd=checkout)
        body = ("## Summary\n**Before:** %s calls an older kit commit.\n**After:** it calls kit commit `%s`.\n"
                "**Why:** repos stay on a supported kit version.\n**Users see:** Nothing changes.\n\n**Details**\nChanged: %s\n"
                % (repo, sha, ", ".join("`%s`" % c for c in changed)))
        print(run("gh", "pr", "create", "-R", repo, "--base", base, "--head", branch, "--title", "Bump agent kit to %s" % sha[:12], "--body", body))
    finally:
        shutil.rmtree(tmp)
    return 0
