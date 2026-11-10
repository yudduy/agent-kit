# Verify: how the kit checks a PR through the real app and its stored data

The kit's reusable workflow [`verify.yml`](../.github/workflows/verify.yml) runs on every PR of an onboarded repo. It starts the PR's app against a freshly seeded database, drives the journeys from the base branch, and checks the stored data directly. It writes `evidence.json` ([schema](../schemas/evidence.v1.json), contract C2 in the [design record](design/2026-11-10-agent-kit.md)) and attests it. The gate trusts the attestation, not the file's word.

## What runs where

| Job | Holds | Runs |
|---|---|---|
| `build` | read-only token, no secrets | the policy's `app.build` on head (and on base when the PR adds regression specs); hands over only the images |
| `run` | read-only token, no secrets | kit code; the candidate app inside its own container; base journeys; PR regression specs inside the Playwright container; publishes the evidence hash as a job output |
| `attest` | `id-token: write`, `attestations: write` | kit code only: checks the evidence against the `run` job's hash, the schema, this run and the trace hashes, then attests it |

- Policy, journeys and seed always come from the **base** commit (INV-3). A PR that edits them changes nothing until it merges.
- Data assertions are run by the kit's observer (`verify/observe.py`) with a read-only connection straight to the database. Candidate code never answers for its own data.
- New spec files the PR adds that match `verify.regress_glob` in the policy (default `tests/regress/*.spec.ts`) run on base and on head. Their result (`fail_on_base`, `pass_on_head`) is shown to humans and feeds the shadow verdict. It never passes the gate alone (`trusted: false`).
- A scenario passes only if its UI step passes **and** it has at least one data assertion, all passing. The evidence `result` is `pass` only if there is at least one scenario and all pass.

## The `app` section of `.agent-kit/policy.yml`

```yaml
app:
  build: "docker build -t app ."          # must leave an image tagged `app`
  start: {port: 3000, health: /healthz}   # the kit runs the image with --network host
  db: {image: postgres:16, seed: .agent-kit/seed/seed.sql}
```

- `build`: a shell command, run in the checked-out tree in the `build` job. In CI it must produce a local image tagged `app`. It runs once for head, and once for base only when the PR adds regression specs.
- `start.port`: the app must listen on this port on `127.0.0.1`.
- `start.health`: a path that returns HTTP 200 once the app is ready (default `/`). The kit waits up to 60 s.
- `start.cmd`: optional, used only by local host-mode reruns (below). CI ignores it.
- The kit passes two environment variables to the app: `PORT` and `DATABASE_URL`.
- `db.seed`: a SQL file loaded into a fresh database before each scenario and each regression run. Synthetic data only (D9).

Regression specs the PR adds are found with an optional glob (quote it; the kit's YAML parser rejects a plain scalar starting with `*`):

```yaml
verify:
  regress_glob: "tests/regress/*.spec.ts"   # the default; `**` matches any depth
```

Supported databases:

| Policy | Engine | `DATABASE_URL` the app gets |
|---|---|---|
| `db: {image: postgres:<tag>, seed: …}` | Postgres, read through `psql` | `postgresql://postgres:agentkit@127.0.0.1:5432/agent_kit_<n>` |
| `db: {engine: sqlite, seed: …}` | SQLite, read through Python's `sqlite3` | `sqlite:///<absolute path>` (the directory is mounted into the app container) |

Any other database (MySQL, MongoDB, Redis, hosted services) is **unsupported**: the run stops with an error. Adding one is later work.

## Writing a journey

A journey is a Playwright spec plus at least one data assertion, both on main:

```yaml
journeys:
  - id: save-profile
    areas: ["src/profile/**"]
    spec: .agent-kit/journeys/save-profile.spec.ts
    data_assertions:
      - {id: name-persisted, sql: "select name from profiles where id = 'seed-user-1'", expect: [{name: "Ada"}]}
```

```ts
import { test, expect } from '@playwright/test';

test('rename the profile', async ({ page }) => {
  await page.goto('/');                      // baseURL is set by the kit
  await page.getByLabel('Name').fill('Ada');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.locator('#status')).toHaveText('Saved');
});
```

- The spec drives the UI only. Whether the data was really stored is the data assertion's job, run after the app stops.
- The spec may import only `@playwright/test` (the kit's own install, version pinned in `verify/local_run.py`). It cannot import the repo's code or packages.
- Each assertion's `sql` must be a single `select`. The observer's connection is read-only, so writes fail. `expect` is the exact list of rows, in order, with column names as keys. Use `order by` when more than one row comes back.
- Each journey gets its own freshly seeded database, so journeys do not depend on each other.

## Rerunning locally

`verify/local_run.py` is the same code the `build` and `run` jobs run (`--phase build` and `--phase observe`; the default `--phase all` does both). Locally, use host mode when Docker is not available: the app starts from `app.start.cmd` and regression specs run on your machine. Host-mode evidence is never attested, so the gate never accepts it.

```bash
# all journeys plus the PR's regression specs
python3 verify/local_run.py --repo ~/src/my-app --base origin/main --head HEAD --out /tmp/evidence --isolation host

# one scenario only
python3 verify/local_run.py --repo ~/src/my-app --base origin/main --head HEAD --out /tmp/evidence \
  --isolation host --scenario save-profile

# check the result
python3 verify/evidence.py /tmp/evidence/evidence.json --artifacts-dir /tmp/evidence
npx playwright show-trace /tmp/evidence/traces/save-profile.zip
```

- Needs Python 3.11+, git, Node and npm (the first run installs Playwright into the scratch directory). Postgres also needs `psql`, plus `AGENT_KIT_PG_URL` (an admin URL to a scratch server) in host mode.
- Exit code: 0 = pass, 1 = fail, 2 = the evidence file itself is invalid.

To see the whole flow on the bundled fixture app, whose planted bug answers "Saved" without writing:

```bash
python3 tests/verify/fixtures/make_repo.py /tmp/fixture     # prints the bug, fix and mutant commits
AGENT_KIT_E2E=1 python3 -m unittest discover -s tests/verify
```
