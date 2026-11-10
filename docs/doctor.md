# Doctor: check a repo for production credentials and missing kit files

`agent-kit doctor` (today `python3 doctor/doctor.py`) enforces INV-7 in the [invariants](design/invariants.md): no production credential where agents or verifiers run.

```bash
python3 doctor/doctor.py <repo> [--env-file dump.env] [--env-names names.txt]
```

- `<repo>`: a checkout of the onboarded repo (default `.`).
- `--env-file`: an env dump of `KEY=VALUE` lines, for example from a Cursor machine. Repeatable.
- `--env-names`: a list of secret names, one per line, for places that show names but not values (Cursor secrets, Actions secrets). Repeatable.

It scans `.cursor/environment.json`, every `.github/workflows/*.yml` and the files you pass, and flags:

| Finding | Example |
|---|---|
| Stripe live secret or restricted key | `sk_live_…`, `rk_live_…` |
| Supabase secret or service-role key | `sb_secret_…`, a JWT with `"role": "service_role"` |
| Hosted Supabase project URL | `https://<ref>.supabase.co` |
| Vercel production binding | `VERCEL_ENV=production` |
| Database URL with a remote host | any `postgres://`, `mysql://`, `mongodb://`, `redis://` host other than localhost or a single-word service name |
| Prod-looking secret name | names with `PROD` or `PRODUCTION`, `SERVICE_ROLE`, `VERCEL_TOKEN`, `SUPABASE_ACCESS_TOKEN` |

It also checks the repo has the kit files (`.agent-kit/policy.yml`, `.agent-kit/journeys/`, `.github/workflows/agent-verify.yml` pinned to a full commit SHA, `.cursor/environment.json`, `.cursor/skills/verify-<app>/`, `docs/features/`, CODEOWNERS), and that `AGENTS.md` records Seer and the native Cursor Sentry trigger as off with this exact line (D4):

```
<!-- agent-kit:automation v1 seer:off cursor-sentry:off -->
```

It never prints a secret value, makes no network calls, and exits 1 on any finding, 0 when clean.
