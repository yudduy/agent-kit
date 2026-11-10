# Feature docs automation (design D8)

After a PR merges on `main`, a Cursor Automation updates `docs/features/<feature>.md` for each feature the PR
touched and opens one `docs` PR. Grok Bot only reads these files. `agent-kit init` cannot create the automation
(Cursor has no public API for it), so set it up once by hand at cursor.com/automations.

## Setup

1. New automation, name `agent-kit feature docs {{REPO}}`.
2. Trigger: GitHub, **Pull request merged**, repository `{{REPO}}`. No other trigger.
3. Repository: single repository, `{{REPO}}`, branch `main`.
4. Tools: keep **Pull request creation** on. Turn off Comment on pull request approvals, Request reviewers, Send to
   Slack, Read Slack channels, MCP servers, Memories and Computer use.
5. Model: Composer or Grok (the included pool).
6. Prompt: paste the block under "Prompt" below, unchanged.
7. Save, activate, then press **Run Test** on a merged PR and check that a docs PR opens, or that the run stops for
   a docs PR.

## Recursion guard

The trigger fires on every merge into any branch, including the docs PRs this automation opens. The prompt's first
step stops the run when the merged PR's base is not `main`, its head branch starts with `agent-kit/docs-`, or it
only touches `docs/features/`. A derived docs PR never starts another docs update (D7).

## Known limits

- On a personal Cursor account the automation runs as you, so its docs PRs are opened by your GitHub account.
- Cursor may not start a run when the merge cannot be linked to the automation owner's GitHub account, as when the
  gate App merges. Check the run history after the first gate merge.

## Prompt

```
A pull request was just merged in {{REPO}}. Keep docs/features/ true to the code.

1. Stop, change nothing and open no pull request if any of these hold:
   - the merged PR's base branch is not main;
   - its head branch starts with agent-kit/docs-;
   - every file it changed is under docs/features/.
2. Read the merged diff (the PR's merge commit against its first parent) and docs/features/README.md.
3. Find the features the diff touched: a docs/features/<feature>.md whose "where its code lives" paths match a changed
   file. If a changed area has no feature file and is a user-facing feature, add docs/features/<feature>.md using the
   README's sections. Ignore changes with no user-facing effect (tests only, CI, formatting).
4. For each touched feature, update only what the diff changed: what it does for a user, where its code lives,
   which journey checks it, and known past bugs (link the merged PR when it fixed a bug). Write plain sentences for
   a reader outside the team. No commit hashes, internal IDs or code snippets. Keep everything else as it is.
5. Change only files under docs/features/. If nothing needs to change, stop and open no pull request.
6. Commit on a new branch named agent-kit/docs-<merged PR number> and open one pull request to main titled
   "Update feature docs for #<merged PR number>". The body says which features changed and why, in one line each,
   and links the merged PR. Do not approve, merge or request reviewers.
```

## What the gate does with it

The PR touches only `docs/features/`, so its class is `docs`, which is on the auto-merge list: the gate merges it
when every required check passes. A PR that touches anything else is classed by the most restrictive path and waits
for a human.
