"""Run the verify flow for one PR: what the `build` and `run` jobs of verify.yml do, minus attestation.

Inputs come from the base commit only (policy, journeys, seed). The candidate (head)
app runs under kit control; data assertions run from kit code straight against the DB.
The PR's new regression specs (policy `verify.regress_glob`, default
tests/regress/*.spec.ts) run on base and on head as untrusted evidence.

Isolation:
- container (default, used in CI): the app is the image `app.build` tags `app`, run
  with `docker run`; regression specs run in the Playwright image. In CI the build
  runs in its own job (`--phase build`), so candidate code never runs on the host
  that writes evidence.json (`--phase observe`).
- host (local reruns only): the app runs from `app.start.cmd` and regression specs
  run on the host. Evidence from host mode is never attested, so the gate never
  accepts it.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gate import miniyaml  # noqa: E402
from verify import evidence, observe  # noqa: E402

PLAYWRIGHT_VERSION = "1.65.0"
PLAYWRIGHT_IMAGE = f"mcr.microsoft.com/playwright:v{PLAYWRIGHT_VERSION}-noble"
KIT_REPO = "yudduy/agent-kit"
DEFAULT_REGRESS_GLOB = "tests/regress/*.spec.ts"
JOURNEYS_DIR = ".agent-kit/journeys"
SPEC_TIMEOUT_S = 600
PG_PASSWORD = "agentkit"


def sh(cmd, **kw):
    print("+", cmd if isinstance(cmd, str) else " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def export_tree(repo, sha, dest):
    dest.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(repo), "archive", sha], check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)


def load_policy(path):
    try:
        policy = miniyaml.loads(Path(path).read_text())
    except miniyaml.YamlError as exc:
        raise SystemExit(f"policy: {exc}")
    app = policy.get("app") or {}
    for key in ("start", "db"):
        if not isinstance(app.get(key), dict):
            raise SystemExit(f"policy: app.{key} is required")
    if "port" not in app["start"] or "seed" not in app["db"]:
        raise SystemExit("policy: app.start.port and app.db.seed are required")
    return policy


def regress_glob(policy):
    return (policy.get("verify") or {}).get("regress_glob", DEFAULT_REGRESS_GLOB)


def new_regression_specs(repo, base_sha, head_sha, glob):
    """Spec files the PR adds (absent on base) that match the policy glob."""
    return git(repo, "diff", "--name-only", "--diff-filter=A", base_sha, head_sha, "--",
               f":(glob){glob}").splitlines()


def journeys_sha(repo, base_sha):
    """Tree sha of the base journeys dir, or None when base has none (C2)."""
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{base_sha}:{JOURNEYS_DIR}"],
                          capture_output=True, text=True)
    return proc.stdout.strip() or None


def db_engine(db):
    if db.get("engine") == "sqlite":
        return "sqlite"
    if str(db.get("image", "")).startswith("postgres"):
        return "postgres"
    raise observe.Unsupported(f"policy app.db {db!r}: only postgres images and engine: sqlite are supported")


def image_digest(ref):
    out = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


class Databases:
    """Hands out a fresh, seeded database per app run."""

    def __init__(self, engine, db, seed_sql, work, isolation):
        self.engine, self.seed_sql, self.work, self.count = engine, seed_sql, work, 0
        self.container, self.digest, self.admin = None, None, os.environ.get("AGENT_KIT_PG_URL")
        if engine == "postgres" and not self.admin:
            if isolation != "container":
                raise SystemExit("postgres in host mode needs AGENT_KIT_PG_URL (an admin URL to a scratch server)")
            self.container = "agent-kit-db"
            sh(["docker", "run", "-d", "--rm", "--name", self.container, "--network", "host",
                "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}", db["image"]], stdout=subprocess.DEVNULL)
            self.digest = image_digest(db["image"])
            self.admin = f"postgresql://postgres:{PG_PASSWORD}@127.0.0.1:5432/postgres"
            self._wait_for_postgres()

    def _wait_for_postgres(self):
        for _ in range(60):
            if subprocess.run(["pg_isready", "-d", self.admin], capture_output=True).returncode == 0:
                return
            time.sleep(1)
        raise SystemExit("postgres did not become ready in 60 s")

    def fresh(self):
        self.count += 1
        if self.engine == "sqlite":
            path = self.work / "db" / f"run{self.count}.sqlite"
            path.parent.mkdir(exist_ok=True)
            path.unlink(missing_ok=True)
            url = f"sqlite://{path}"
        else:
            name = f"agent_kit_{self.count}"
            observe.psql(self.admin, "-c", f"drop database if exists {name}", "-c", f"create database {name}")
            url = self.admin.rsplit("/", 1)[0] + f"/{name}"
        observe.load_seed(self.engine, url, self.seed_sql)
        return url

    def close(self):
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)


class App:
    """Builds one side (base or head) and starts it against a given database."""

    def __init__(self, app, src, side, isolation, work):
        self.app, self.src, self.side, self.isolation, self.work = app, src, side, isolation, work
        self.port = int(app["start"]["port"])
        self.health = app["start"].get("health", "/")
        self.proc = None
        if isolation == "host" and not app["start"].get("cmd"):
            raise SystemExit("host isolation needs app.start.cmd in the policy")

    @property
    def image(self):
        return f"agent-kit-{self.side}"

    def build(self):
        """Runs candidate code (e.g. Dockerfile steps); in CI only in the `build` job."""
        if self.app.get("build"):
            sh(self.app["build"], shell=True, cwd=self.src)
        if self.isolation == "container":
            sh(["docker", "tag", "app", self.image])

    def start(self, db_url):
        env = {"PORT": str(self.port), "DATABASE_URL": db_url}
        if self.isolation == "host":
            log = open(self.work / f"app-{self.side}.log", "ab")
            self.proc = subprocess.Popen(self.app["start"]["cmd"], shell=True, cwd=self.src, stdout=log,
                                         stderr=subprocess.STDOUT, env={**os.environ, **env},
                                         start_new_session=True)
        else:
            cmd = ["docker", "run", "-d", "--rm", "--name", f"agent-kit-app-{self.side}", "--network", "host"]
            for k, v in env.items():
                cmd += ["-e", f"{k}={v}"]
            if db_url.startswith("sqlite:"):
                db_dir = str(Path(observe.sqlite_path(db_url)).parent)
                cmd += ["-v", f"{db_dir}:{db_dir}"]
            sh(cmd + [self.image], stdout=subprocess.DEVNULL)
        return self._wait_healthy()

    def _wait_healthy(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}{self.health}", timeout=2) as r:
                    if r.status == 200:
                        return True
            except OSError:
                pass
            if self.proc and self.proc.poll() is not None:
                break
            time.sleep(0.5)
        print(f"app ({self.side}) did not become healthy at {self.url}{self.health}")
        return False

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.proc:
            os.killpg(self.proc.pid, 15)
            self.proc.wait(timeout=10)
            self.proc = None
        elif self.isolation == "container":
            subprocess.run(["docker", "rm", "-f", f"agent-kit-app-{self.side}"], capture_output=True)


class Playwright:
    """The kit's own Playwright install; specs never resolve the candidate's node_modules."""

    def __init__(self, work, install_browsers):
        self.root = work / "pw"
        cli = self.root / "node_modules" / "@playwright" / "test" / "cli.js"
        if not cli.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            sh(["npm", "install", "--no-save", "--no-package-lock", "--ignore-scripts", "--prefix",
                str(self.root), f"@playwright/test@{PLAYWRIGHT_VERSION}"], stdout=subprocess.DEVNULL)
        if install_browsers:
            sh(["node", str(cli), "install", "--with-deps", "chromium"])

    def stage(self, group, name, src):
        """Copy a spec under the kit's install so `@playwright/test` resolves to it."""
        dest = self.root / group / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest

    def run(self, spec, base_url, name, in_container=False):
        """Run one spec; return (passed, trace path or None)."""
        out = self.root.parent / "results" / name
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        root = Path("/kit") if in_container else self.root
        spec_in = root / spec.relative_to(self.root)
        config = spec.parent / f"{spec.name}.config.mjs"
        config.write_text("export default " + json.dumps({
            "testDir": str(spec_in.parent), "testMatch": [spec.name], "timeout": 60000, "retries": 0,
            "workers": 1, "reporter": "line", "use": {"baseURL": base_url, "trace": "on"}}) + ";\n")
        out_in = "/out" if in_container else str(out)
        args = ["node", str(root / "node_modules/@playwright/test/cli.js"), "test",
                "-c", str(spec_in.parent / config.name), "--output", f"{out_in}/results"]
        if in_container:
            args = ["docker", "run", "--rm", "--name", "agent-kit-regress", "--network", "host",
                    "-v", f"{self.root / 'node_modules'}:/kit/node_modules:ro",
                    "-v", f"{spec.parent}:{spec_in.parent}:ro", "-v", f"{out}:/out", "-w", "/kit",
                    PLAYWRIGHT_IMAGE, *args]
        print("+", " ".join(args), flush=True)
        try:
            passed = subprocess.run(args, cwd=self.root, timeout=SPEC_TIMEOUT_S).returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
        finally:
            if in_container:
                subprocess.run(["docker", "rm", "-f", "agent-kit-regress"], capture_output=True)
        traces = sorted(out.rglob("trace.zip"))
        return passed, traces[0] if traces else None


def run_scenario(journey, app, dbs, pw, spec_src, out_dir):
    db_url = dbs.fresh()
    spec = pw.stage("journeys", Path(journey["spec"]).name, spec_src / journey["spec"])
    ui_pass, trace = False, None
    if app.start(db_url):
        ui_pass, trace = pw.run(spec, app.url, journey["id"])
    app.stop()
    assertions = observe.run_assertions(dbs.engine, db_url, journey.get("data_assertions") or [])
    ui_result = "pass" if ui_pass else "fail"
    trace_name = None
    if trace:
        trace_name = f"traces/{journey['id']}.zip"
        (out_dir / "traces").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(trace, out_dir / trace_name)
    return {"id": journey["id"], "source": "base", "ui_result": ui_result, "data_assertions": assertions,
            "result": evidence.scenario_result(ui_result, assertions), "trace": trace_name}


def run_regression(spec, app, dbs, pw):
    """Untrusted: the spec comes from the PR. In container mode it runs in the Playwright image."""
    passed = False
    if app.start(dbs.fresh()):
        passed, _ = pw.run(spec, app.url, f"regress-{app.side}-{spec.name}",
                           in_container=app.isolation == "container")
    app.stop()
    return passed


def runner_image():
    if os.environ.get("ImageOS"):
        return f"{os.environ['ImageOS']}/{os.environ.get('ImageVersion', 'unknown')}"
    return f"local/{platform.system().lower()}-{platform.machine()}"


def verify(args):
    repo = Path(args.repo).resolve()
    base_sha, head_sha = git(repo, "rev-parse", args.base), git(repo, "rev-parse", args.head)
    work = Path(args.work or tempfile.mkdtemp(prefix="agent-kit-verify-")).resolve()
    out_dir = Path(args.out).resolve()
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(f"--out {out_dir} is not empty; choose a new directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    for side in ("base", "head"):
        if (work / side).exists():
            shutil.rmtree(work / side)
    base_dir, head_dir = work / "base", work / "head"
    export_tree(repo, base_sha, base_dir)
    export_tree(repo, head_sha, head_dir)

    policy = load_policy(base_dir / ".agent-kit" / "policy.yml")
    app_cfg = policy["app"]
    engine = db_engine(app_cfg["db"])
    seed = (base_dir / app_cfg["db"]["seed"]).read_bytes()
    journeys = policy.get("journeys") or []
    if args.scenario:
        journeys = [j for j in journeys if j["id"] in args.scenario]
        if not journeys:
            raise SystemExit(f"no journey with id {args.scenario} in the base policy")
    new_specs = [] if args.scenario else new_regression_specs(repo, base_sha, head_sha, regress_glob(policy))

    head = App(app_cfg, head_dir, "head", args.isolation, work)
    base = App(app_cfg, base_dir, "base", args.isolation, work)
    apps = [head, base] if new_specs else [head]
    if args.phase == "observe":
        sh(["docker", "load", "-i", args.images])
    else:
        for app in apps:
            app.build()
    if args.phase == "build":
        sh(["docker", "save", "-o", args.images, *[app.image for app in apps]])
        return 0
    if args.isolation == "container":
        # The candidate's build (or its image archive) can set any local tag; re-pull the trusted images.
        trusted = [app_cfg["db"]["image"]] if engine == "postgres" else []
        trusted += [PLAYWRIGHT_IMAGE] if new_specs else []
        for image in trusted:
            sh(["docker", "pull", "-q", image], stdout=subprocess.DEVNULL)

    pw = Playwright(work, args.install_browsers)
    dbs = Databases(engine, app_cfg["db"], seed.decode(), work, args.isolation)
    try:
        scenarios = [run_scenario(j, head, dbs, pw, base_dir, out_dir) for j in journeys]
        staged = [pw.stage("regress", Path(s), head_dir / s) for s in new_specs]
        on_head = [run_regression(s, head, dbs, pw) for s in staged]
        on_base = [run_regression(s, base, dbs, pw) for s in staged]
    finally:
        head.stop()
        base.stop()
        dbs.close()

    kit_ref = args.kit_ref or git(Path(__file__).resolve().parent.parent, "rev-parse", "HEAD")
    doc = {
        "schema": 1, "repo": args.repo_name, "pr": args.pr, "head_sha": head_sha, "base_sha": base_sha,
        "kit_ref": kit_ref,
        "policy_sha": git(repo, "rev-parse", f"{base_sha}:.agent-kit/policy.yml"),
        "journeys_sha": journeys_sha(repo, base_sha),
        "run": {"id": args.run_id, "attempt": args.run_attempt,
                "workflow_ref": args.workflow_ref or f"{KIT_REPO}/.github/workflows/verify.yml@{kit_ref}"},
        "env": {"runner_image": runner_image(),
                "app_image_digest": image_digest(head.image) if args.isolation == "container" else None,
                "db_image_digest": dbs.digest, "seed_hash": "sha256:" + evidence.sha256_bytes(seed)},
        "scenarios": scenarios,
        "regression": [evidence.regression_entry(s, b, h) for s, b, h in zip(new_specs, on_base, on_head)],
        "artifacts": [{"name": s["trace"], "sha256": evidence.sha256_file(out_dir / s["trace"])}
                      for s in scenarios if s["trace"]],
        "result": evidence.overall_result(scenarios),
    }
    (out_dir / "evidence.json").write_text(json.dumps(doc, indent=2) + "\n")
    problems = evidence.errors(doc, out_dir)
    for line in problems:
        print(f"evidence: {line}", file=sys.stderr)
    print(f"evidence written to {out_dir / 'evidence.json'}: result {doc['result']}")
    if problems:
        return 2
    return 0 if doc["result"] == "pass" else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=".", help="git checkout holding both the base and head commits")
    p.add_argument("--base", required=True, help="base commit (policy, journeys and seed come from here)")
    p.add_argument("--head", required=True, help="candidate commit")
    p.add_argument("--out", required=True, help="output dir for evidence.json and traces/")
    p.add_argument("--work", help="scratch dir (default: a new temp dir)")
    p.add_argument("--repo-name", default="local/repo", help="owner/name for the evidence file")
    p.add_argument("--pr", type=int, default=1)
    p.add_argument("--run-id", type=int, default=0, help="0 means a local run")
    p.add_argument("--run-attempt", type=int, default=1)
    p.add_argument("--kit-ref", help="kit commit (default: this checkout's HEAD)")
    p.add_argument("--workflow-ref", help="default: yudduy/agent-kit/.github/workflows/verify.yml@<kit-ref>")
    p.add_argument("--scenario", action="append", help="run only this journey id (repeatable)")
    p.add_argument("--isolation", choices=("container", "host"), default="container")
    p.add_argument("--phase", choices=("all", "build", "observe"), default="all",
                   help="CI splits the run: `build` saves the candidate images to --images, `observe` loads them")
    p.add_argument("--images", help="image archive written by --phase build and read by --phase observe")
    p.add_argument("--install-browsers", action="store_true", help="install Chromium and its system deps (CI)")
    args = p.parse_args(argv)
    if args.phase != "all" and (args.isolation != "container" or not args.images):
        p.error("--phase build/observe needs --isolation container and --images")
    return verify(args)


if __name__ == "__main__":
    sys.exit(main())
