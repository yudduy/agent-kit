# agent-gate: setup and operation

The gate decides every open pull request in every onboarded repo and posts the `agent-gate` check (contract C5 in the [design record](design/2026-11-10-agent-kit.md)). When the check passes, the gate merges the pull request (D2). A change passes without a human only if its class is on the repo's auto list (phase 1: `docs`, `kit-bump`); every other class needs an approval from a listed human, not its author or a committer, on the exact head commit.

It runs as a scheduled workflow in private `yudduy/agent-control`, every 5 minutes, and reads everything through the GitHub API. It never checks out, builds or runs pull request code (INV-2). Policy and journeys come from the default branch tip, never the pull request (INV-3); a pull request into any other branch fails.

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

5. **Per onboarded repo, apply the ruleset only after its onboarding PR has merged.** Until the default branch has a valid `.agent-kit/policy.yml` the gate fails every PR, so a ruleset applied earlier blocks the onboarding PR itself. Then add the ruleset from [`gate/ruleset.json`](../gate/ruleset.json): `agent-gate` required and bound to the App, strict up-to-date branches, no bypass actors, admins included (INV-1):

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

- Reads, per open PR: the base branch tip, `compare/<base tip>...<head>` (paths and patches), `.agent-kit/policy.yml` and the `.agent-kit/journeys` tree sha at the base tip, the kit callers in `.github/workflows/` at the base tip, check runs on head, reviews, PR commits, and the evidence artifact.
- Required checks: each policy entry is `{name: <check run name>, app: <app slug>}`, for example `{name: test, app: github-actions}` (an Actions check run is named after its job). Only the latest check run with that name from that app counts; a run from any other app, or a commit status, counts as missing.
- Kit pins (D1): every `uses: yudduy/agent-kit/...@<ref>` in the base branch's workflows must equal the policy's `kit_ref`; any other ref fails the gate.
- Evidence: the newest non-expired artifact named `agent-kit-evidence` from a `pull_request` run on the head SHA. It holds `evidence.json` at its root plus every file listed in `artifacts`. The gate runs `gh attestation verify evidence.json --repo OWNER/REPO --signer-workflow yudduy/agent-kit/.github/workflows/verify.yml --signer-digest <base kit_ref> --deny-self-hosted-runners`, then checks the attested run id and every C2 field against base.
- Writes, in this order: one JSON line appended to `decisions/<UTC date>.jsonl` on the `decisions-log` branch (with the workflow's own `GITHUB_TOKEN`), then the `agent-gate` check run on head, both only when the decision changed; then a merge with `sha=head` for every passing PR (auto class or human-approved), retried each tick until it lands. If the log write fails, nothing is posted or merged for that PR.
- The App installation token is minted per repo and scoped to that repo. The App JWT is signed RS256 by the `openssl` CLI (the Python standard library has no RSA; `openssl` and `gh` ship on GitHub-hosted runners).

## Emergency pause

```sh
gh variable set AGENT_KIT_PAUSED -R yudduy/agent-control -b true    # resume with -b false
```

While paused the gate still posts verdicts, but every check is `failure` with the reason "the gate is paused", so nothing merges. To stop the gate entirely, disable the workflow: `gh workflow disable agent-gate -R yudduy/agent-control`.

## Policy file notes

- The gate parses a YAML subset with the standard library: block maps and lists, one-line `[...]` and `{...}`, quoted and plain scalars, `#` comments. Anchors, aliases, tags, `|`/`>` blocks and multi-line flow are rejected, which fails the gate. Quote globs that start with `*`, such as `"*.md"`.
- Repo classes match by `paths` (globs, `**` crosses folders) and/or `added_lines` (Python regexes on added lines).
- The kit floor is hard-coded in `gate/policy.py` and `gate/classify.py`; `floor_extra` can only add paths. Governance always covers `.agent-kit/**`, `.github/**`, `.cursor/**` (verify skills), `CODEOWNERS`, `docs/CODEOWNERS`, every `AGENTS.md` and `CLAUDE.md`, any `fixtures/` or `evals/` folder, the policy's seed file and journey specs (INV-9, INV-12). Add other grader or fixture paths with `floor_extra: {governance: {paths: [...]}}`.
- Renames count both paths: moving a file out of a class still counts as touching it.

## Run locally

```sh
python3 -m unittest discover -s tests/gate -t .           # all gate tests, standard library only
python3 -m gate.main --dry-run tests/gate/fixtures/tick    # one tick against JSON fixtures; prints the writes
```

A fixture directory holds `api.json` (each `GET` path mapped to its JSON response), `artifacts/<artifact id>/` (the artifact's files) and `attestations.json` (evidence sha256 → the verified signer workflow, signer digest and run URI). Set `AGENT_GATE_APP_ID` to let the dry run skip decisions it already posted.

## Known limits

- Merge queues are not supported: the gate merges through the merge API and does not run on `merge_group`.
- Fork PRs fail closed. Changes with 300 or more files, or pull requests with 250 or more commits, fail closed (the API stops listing them there).
- A slow regex in a repo class's `added_lines` (written in a human-approved policy) can stall a tick until the 15-minute job timeout; the gate then posts nothing new, so nothing merges.
- A file with no visible patch (binary or very large) counts as `review`, so a docs PR that adds an image needs an approval.
