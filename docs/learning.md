# Learning loop

Weekly, `learn.yml` in agent-control runs `python3 -m learning.harvest`, appends new cases to `learning/cases.jsonl`
on the `decisions-log` branch, and starts one Cursor proposal run for each class that reached 2 cases (D10).
Learning takes effect only through a PR a human merges (INV-11).

## Signals (C6 `signal`)

| Signal | Source |
|---|---|
| `duy_edit` | A human (not the PR author, not a bot) edited an agent PR's body. |
| `review_comment` | A human review or comment on a merged agent PR that raises a known topic; "LGTM" is skipped. |
| `revert_14d` | A PR merged within 14 days that reverts the agent PR, or references it as a fix-up. |
| `gate_failure` | An `agent-gate` check that failed on the agent PR; the class comes from the first gate reason. |
| `false_green` | The gate passed, then within 14 days the PR was reverted or its `fp` incident reopened. |

An agent PR is one authored by a bot or a login in `agent_logins`, or one on a `cursor/` branch. All queries are
read-only GraphQL; the only write is the workflow's commit to `decisions-log`. Case ids are stable, so reruns never
duplicate a case. Line schema: [`schemas/learning-case.v1.json`](../schemas/learning-case.v1.json).

## Classes

Each case gets a `proposed_class` slug: `pr-description`, `data-not-persisted`, `missing-test`, `scope`,
`reverted-fix`, `false-green`, or `gate-<reason>`. A (repo, class) pair qualifies at 2 cases and is proposed once,
in the week it crosses the line. The proposal run gets [`learning/propose.md`](../learning/propose.md) with the cases.

## The proposal agent

It uses pstack `correct` and `reflect` (on the Mac mini from `~/.agents/skills/`, in Cursor from pstack cloned at the
kit's pinned commit), fixes the class at the highest level that works (check, then verify skill, then eval case,
then skill text), and must prove the fix on a case from a real failure: fails before, passes after, full suite
green (INV-13).

## Path check (INV-12)

`learning/pathcheck.py` runs from the pinned kit in each repo's `agent-learning-check.yml` on PRs labelled
`agent:learning` or carrying the `agent-kit:learning v1` marker. It reads the PR's file list from the API and fails
if anything other than an added test or eval case changes: policy, journeys, gate, verify, CI, graders and
thresholds fail on any touch; existing case files may only gain lines.
