# Onboarding a repo

`agent-kit init <owner/repo>` opens one draft PR that adds the kit to a repo, then starts one Cursor run that
proposes the repo's 3 to 5 key journeys on the same branch. Duy reviews the journeys and merges.

## Install

```sh
git clone https://github.com/yudduy/agent-kit ~/Projects/agent-kit
ln -s ~/Projects/agent-kit/bin/agent-kit ~/.local/bin/agent-kit   # any directory on PATH
```

Needs Python 3.11+, `git`, and `gh` logged in as an account that can push to the repo.
The command is `agent-kit`, not `gh agent-kit`: a gh extension must live in a repo named `gh-*`, and this repo is
`agent-kit`, so the record's `gh agent-kit` spelling is not used.

## Commands

| Command | What it does |
|---|---|
| `agent-kit init owner/repo --dry-run` | Prints the files, branch and PR it would create. Clones nothing and calls no API. |
| `agent-kit init owner/repo` | Shallow-clones the repo, adds the template files, pushes `agent-kit/onboard`, opens one draft PR, starts the journeys run. |
| `agent-kit journeys owner/repo` | Starts the journeys run on `agent-kit/onboard` (use it when `init` ran without a Cursor key). |
| `agent-kit sync owner/repo` | Opens one PR that moves `kit_ref` and every `uses: yudduy/agent-kit/...@<sha>` line to the new kit commit. |

`--ref <40-hex>` pins a specific kit commit. Without it, the kit pins this checkout's `HEAD`, which must already be
on `origin/main` (callers cannot use an unpublished commit).

## What `init` writes

- `.agent-kit/policy.yml`: C1 skeleton with `kit_ref` set, approvers `[yudduy]`, auto-merge `[docs]` only, kit caps.
- `.agent-kit/journeys/README.md`: what a journey is; the Cursor run adds the specs.
- `.github/CODEOWNERS`: Duy owns `.agent-kit/`, `.github/`, `.cursor/`, CODEOWNERS, AGENTS.md, CLAUDE.md. If the repo
  already has a CODEOWNERS file, only the missing lines are appended to it.
- `.github/workflows/agent-learning-check.yml`: the INV-12 path check for learning PRs, run from the pinned kit.
- `.github/workflows/agent-verify.yml`: the verify caller (owned by the verify builder), with the kit SHA filled in.
- `.cursor/environment.json`, `docs/features/README.md`.

Every template file may use `{{KIT_REF}}`, `{{REPO}}` and `{{APP}}`; `init` replaces them. Files that already exist
are kept, except `.agent-kit/` with `--force`.

## Refusals

- The repo already has `.agent-kit/`: use `sync`, or `--force` to overwrite.
- No `Dockerfile` at the root: the kit's verify job builds with `docker build`, so the repo cannot be verified yet.

## The journeys run

Uses `CURSOR_API_KEY` (the onboarding key, separate from the dispatcher's). The run works on `agent-kit/onboard`
directly and does not open its own PR. It follows pstack `create-verification-skill` at the kit's pinned commit, writes
`.cursor/skills/verify-<app>/` and `docs/features/`, and adds each journey as a Playwright spec plus a policy entry with
at least one stored-data assertion. Without a key, `init` still opens the PR and prints the exact next command.

## After merge

Add the repo to `agent-kit.json` in agent-control (see [dispatcher.md](dispatcher.md)) so the dispatcher, gate and
harvest pick it up. `init` prints this step; it does not write to agent-control.
