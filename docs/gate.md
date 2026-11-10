# agent-gate: setup and operation

The gate decides every open pull request in every onboarded repo and posts the `agent-gate` check (contract C5 in the [design record](design/2026-11-10-agent-kit.md)). It merges a pull request only when the check passes and the change class is on the repo's auto list (phase 1: `docs`, `kit-bump`). Every other class needs an approval from a listed human on the exact head commit; the gate then turns green and a human merges.

It runs as a scheduled workflow in private `yudduy/agent-control`, every 5 minutes, and reads everything through the GitHub API. It never checks out, builds or runs pull request code (INV-2). Policy and journeys come from the base branch tip, never the pull request (INV-3).

## One-time setup (org owner)

Run these as an owner of the org or account that owns the repos. `APP_ID` is the App's numeric id.

1. **Create the GitHub App `agent-gate`** (Settings → Developer settings → GitHub Apps → New). Webhook: off. Repository permissions:

   | Permission | Access | Used for |
   |---|---|---|
   | Checks | Read and write | post `agent-gate`, read other checks |
   | Contents | Read and write | read base policy, merge with `sha=head` |
   | Pull requests | Read | list PRs, reviews, commits |
   | Actions | Read | find the verify run and download its evidence artifact |
   | Attestations | Read | `gh attestation verify` on private repos |
   | Commit statuses | Read | required checks that report as statuses |
   | Metadata | Read | required by GitHub |

   Install it on the onboarded repos only ("Only select repositories"). Generate a private key.

2. **Store the key in agent-control's protected environment**, limited to `main`:

   ```sh
   R=yudduy/agent-control
   gh api -X PUT repos/$R/environments/merge-gate --input - <<'EOF'
   {"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   EOF
   gh api -X POST repos/$R/environments/merge-gate/deployment-branch-policies -f name=main -f type=branch
   gh secret set AGENT_GATE_PRIVATE_KEY --env merge-gate -R $R < agent-gate.private-key.pem
   gh variable set AGENT_GATE_APP_ID -R $R -b "$APP_ID"
   gh variable set AGENT_KIT_PAUSED -R $R -b false
   ```

   Then delete the downloaded `.pem`. The key must never exist on the Mac mini or any machine that runs PR code (INV-2).

3. **Create the append-only decisions log** in agent-control:

   ```sh
   git switch --orphan decisions-log && git commit --allow-empty -m "Start the gate decisions log" && git push origin decisions-log
   gh api -X POST repos/yudduy/agent-control/rulesets --input - <<'EOF'
   {"name": "decisions-log append-only", "target": "branch", "enforcement": "active", "bypass_actors": [],
    "conditions": {"ref_name": {"include": ["refs/heads/decisions-log"], "exclude": []}},
    "rules": [{"type": "non_fast_forward"}, {"type": "deletion"}]}
   EOF
   ```

4. **Install the workflow**: copy [`templates/agent-control/.github/workflows/gate.yml`](../templates/agent-control/.github/workflows/gate.yml) to agent-control and set `AGENT_KIT_REF` to the full 40-hex agent-kit commit to run. Change it only by pull request.

5. **Per onboarded repo, after its onboarding PR has merged** (the gate fails every PR until the base branch has a valid `.agent-kit/policy.yml`), add the ruleset from [`gate/ruleset.json`](../gate/ruleset.json): `agent-gate` required and bound to the App, strict up-to-date branches, no bypass actors, admins included (INV-1):

   ```sh
   jq --argjson id "$APP_ID" '.rules[0].parameters.required_status_checks[0].integration_id = $id' gate/ruleset.json \
     | gh api -X POST repos/OWNER/REPO/rulesets --input -
   # INV-1 check: expect [] and agent-gate bound to APP_ID
   gh api repos/OWNER/REPO/rulesets --jq '.[] | select(.name=="agent-gate") | .id' \
     | xargs -I{} gh api repos/OWNER/REPO/rulesets/{} --jq '{bypass: .bypass_actors, checks: .rules[].parameters.required_status_checks}'
   ```

6. **Register the repo** in `agent-kit/repos.json` on agent-control `main` (format below).

## Repo registry (`agent-kit/repos.json` in agent-control)

```json
{"v": 1, "repos": ["yudduy/demo", "Layr-Labs/yukon-commons"]}
```

The gate reads it from agent-control `main` through the API. Any other shape stops the tick with an error.

## What the gate reads and writes

- Reads, per open PR: the base branch tip, `compare/<base tip>...<head>` (paths and patches), `.agent-kit/policy.yml` and the `.agent-kit/journeys` tree sha at the base tip, check runs and statuses on head, reviews, PR commits, and the evidence artifact.
- Evidence: the newest non-expired artifact named `agent-kit-evidence` from a `pull_request` run on the head SHA. It holds `evidence.json` at its root plus every file listed in `artifacts`. The gate runs `gh attestation verify evidence.json --repo OWNER/REPO --signer-workflow yudduy/agent-kit/.github/workflows/verify.yml --signer-digest <base kit_ref> --deny-self-hosted-runners`, then checks the attested run id and every C2 field against base.
- Writes: one `agent-gate` check run on head (only when the decision changed), a merge with `sha=head` for passing auto-class PRs, and one JSON line per decision appended to `decisions/<UTC date>.jsonl` on the `decisions-log` branch, using the workflow's own `GITHUB_TOKEN`.
- The App installation token is minted per repo and scoped to that repo. The App JWT is signed RS256 by the `openssl` CLI (the Python standard library has no RSA; `openssl` and `gh` ship on GitHub-hosted runners).

## Emergency pause

```sh
gh variable set AGENT_KIT_PAUSED -R yudduy/agent-control -b true    # resume with -b false
```

While paused the gate still posts verdicts, but every check is `failure` with the reason "the gate is paused", so nothing merges. To stop the gate entirely, disable the workflow: `gh workflow disable agent-gate -R yudduy/agent-control`.

## Policy file notes

- The gate parses a YAML subset with the standard library: block maps and lists, one-line `[...]` and `{...}`, quoted and plain scalars, `#` comments. Anchors, aliases, tags, `|`/`>` blocks and multi-line flow are rejected, which fails the gate. Quote globs that start with `*`, such as `"*.md"`.
- Repo classes match by `paths` (globs, `**` crosses folders) and/or `added_lines` (Python regexes on added lines).
- The kit floor is hard-coded in `gate/policy.py` and `gate/classify.py`; `floor_extra` can only add paths. Declare eval files, graders and Sentry-event fixtures as governance there so learning PRs that touch them need a human (INV-12): `floor_extra: {governance: {paths: ["evals/**", "fixtures/sentry/**"]}}`.

## Run locally

```sh
python3 -m unittest discover -s tests/gate -t .           # all gate tests, standard library only
python3 -m gate.main --dry-run tests/gate/fixtures/tick    # one tick against JSON fixtures; prints the writes
```

A fixture directory holds `api.json` (each `GET` path mapped to its JSON response), `artifacts/<artifact id>/` (the artifact's files) and `attestations.json` (evidence sha256 → the verified signer workflow, signer digest and run URI). Set `AGENT_GATE_APP_ID` to let the dry run skip decisions it already posted.

## Known limits

- Merge queues are not supported: the gate merges through the merge API and does not run on `merge_group`.
- Fork PRs fail closed. Changes with 300 or more files fail closed (the compare API stops listing files there).
- A file with no visible patch (binary or very large) counts as `review`, so a docs PR that adds an image needs an approval.
