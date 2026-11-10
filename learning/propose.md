# Learning proposal: {{CLASS}} in {{REPO}}

You are a fresh agent. People or checks corrected agent work in {{REPO}} the same way at least twice. Your job is to
make the next agent not repeat it, and to prove that your change works on the real failures. Duy reviews and merges;
nothing you learn takes effect any other way (INV-11).

## The cases

Each line is one real signal (contract C6). Open every `evidence` link and read the actual PR, comment, revert or
check before you decide anything.

```jsonl
{{CASES}}
```

## Method

1. Get pstack's `correct` and `reflect` skills. On the Mac mini they are at `~/.agents/skills/correct/SKILL.md` and
   `~/.agents/skills/reflect/SKILL.md`. In a Cursor machine, clone the kit's pinned copy:
   `git clone {{PSTACK_URL}} /tmp/pstack && git -C /tmp/pstack checkout {{PSTACK_SHA}}`,
   then read `/tmp/pstack/skills/correct/SKILL.md` and `/tmp/pstack/skills/reflect/SKILL.md`.
2. Follow `correct` on these cases only: name the one mistake they share, in one sentence. If they do not share one,
   stop and reply with that finding; do not open a PR.
3. Fix it at the highest level that works, in this order: a check that fails on the mistake, then a fix to the repo's
   verify skill (`.cursor/skills/verify-*`), then a new eval or test case, then a small edit to skill text.
4. Use `reflect` on your own draft before you open the PR: is every line needed, and does it fix the class, not just
   these instances?

## What you may change (INV-12)

- You may add new test or eval cases: a new file, or new lines at the end of a `.jsonl` case list. Any edit to
  an existing test file or JSON case file fails the check.
- You must not edit or delete existing expectations, graders, checker thresholds, anything under `.agent-kit/`
  (policy, journeys, seed), the gate, the verify code, or an existing CI workflow. The path check fails a PR that
  does. If the right fix needs one of these, describe the exact change under a `Needs a human` heading instead.
- Anything beyond added cases (a new check, a verify-skill fix, a skill-text edit) is allowed, but the path check
  labels the PR `governance`, so only Duy can approve it. Keep it small.

## Proof (INV-13)

- Build at least one case from a real failure above, quoting its `evidence` link.
- Run it on the base branch and show it fails; run it with your change and show it passes. Paste both commands and
  their output in the PR's Test plan.
- Run the full test and eval suite with your change and paste the result. A regression anywhere means no PR.
- A skill-text edit gets a blind comparison against the old text by a model from another family; its verdict is
  posted on the PR. Say in the PR what the edit should change in an agent's behavior.

## The PR

- Work on a new branch named `learn/{{CLASS}}` (add `-2`, `-3` if it exists) and push it; the required
  `agent-learning-check` runs only on `learn/*` branches. Open the PR from that branch, as a draft.
- Put `<!-- agent-kit:learning v1 class:{{CLASS}} model:{{MODEL}} -->` on the first line of the body.
- Title: an imperative phrase saying what the change makes impossible, under 60 characters.
- Never write to Slack, never push to another person's branch, and never use production credentials.
