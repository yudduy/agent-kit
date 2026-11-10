# Learning loop

Weekly, `learn.yml` in agent-control runs `python3 -m learning.harvest`, appends new cases to `learning/cases.jsonl`
on the `decisions-log` branch, and starts one Cursor proposal run for each class that reached 2 cases (D10).
Learning takes effect only through a PR a human merges (INV-11).

Repos come from the gate's registry, `agent-kit/repos.json` in agent-control. Pass `--agent-login` for each bot
account that authors agent PRs (O2). Proposal runs need `dispatcher/cursor.py` and the pstack pin in
`cli/onboard.py` on the same kit commit.

## Signals (C6 `signal`)

| Signal | Source |
|---|---|
| `duy_edit` | A human (not the PR author, not a bot) edited an agent PR's body. |
| `review_comment` | A human review or comment on a merged agent PR that raises a known topic; "LGTM" is skipped. |
| `revert_14d` | A PR merged within 14 days that reverts the agent PR, or references it as a fix-up. |
| `gate_failure` | An `agent-gate` check that failed on an agent PR updated in the window, merged or not (open, closed, abandoned); the class comes from the first gate reason. |
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

Proposal runs push `learn/<class>` branches. Each onboarded repo's `agent-learning-check` workflow is a required
check: it runs on every PR (so the gate never sees it skipped) and checks only `learn/*` branches with
`learning/pathcheck.py`, which reads the PR's file list from the API and never runs PR code.

| PR changes | Result |
|---|---|
| Only new test or eval files, or lines appended to a `.jsonl` case list | Pass |
| Also a higher-level fix: a new check or workflow, a verify-skill or skill-text edit, other code | Pass, labelled `governance` (only Duy can approve) |
| A deleted or renamed file, an edited existing test, case or test setup file, `.agent-kit/` (policy, journeys, seed), graders, thresholds, `gate/`, `verify/`, `learning/`, or an existing CI workflow | Fail |

An edit to an existing test fails even if it only adds lines, because one added `return` can switch a test off.
A new file that runs when a suite loads (`conftest.py`, `__init__.py`, `*.config.*`, setup files) is not a case:
it can switch existing tests off, so it counts as a higher-level fix and gets the `governance` label.

## Proof check (INV-13)

The same workflow has a second required job, `learning-proof`, which runs on every PR and checks only `learn/*`
branches with `learning/proof.py`. Each test or eval file the PR adds, and each `.jsonl` case list it appends to, is
copied into a checkout of the base commit and run there, then run on the head commit; then the full suite runs on head.

| Run | Must show |
|---|---|
| Case on base | At least one test fails with an `AssertionError` (or `pytest.fail`), no test fails any other way, and there is no collection, setup or teardown error. A missing import or a syntax error is not proof. |
| Case on head | Every test passes, with at least one assertion executed. |
| Full suite on head | Exit 0. |

Supported runner: pytest only, with plain `assert` or unittest `assert*` methods. The kit loads its plugin
`learning/pytest_plugin/agent_kit_proof.py` through `PYTEST_ADDOPTS`, and the plugin writes each test's outcome and the
number of assertions that passed. A case command that writes no report (Jest, Vitest, Playwright, a plain script)
fails the proof, because their JSON reporters give no executed-assertion count. The job's summary and a notice give
the counts. The repo sets the commands as Actions variables: `AGENT_KIT_CASE_CMD` (a pytest command with `{case}`),
`AGENT_KIT_SUITE_CMD`, and optionally `AGENT_KIT_TEST_SETUP` (run in both checkouts first). This job runs PR code, so it
has only read permissions and no secrets; PR code runs in the same process as the plugin, so a human still reviews.

## Blind review of skill text

The daily `judge` job in `learn.yml` runs `python3 -m learning.judge`. For each open `learn/*` PR that adds or
edits skill or instruction text (`SKILL.md`, `AGENTS.md`, `CLAUDE.md`, Markdown under a `skills/` folder, files under
`.cursor/rules/`), it shows the old and new text to an OpenAI model (variable `AGENT_KIT_JUDGE_MODEL`) as Version A
and Version B, in an order taken from the head commit, without saying which is new. An added file's old text is
`(no file)`, so the judge decides whether the new text is better than having none. It posts one verdict comment per
head commit: new better, old better, tie, or unclear. Proposals run on a
Cursor model, so the judge is from another family; if the PR's learning marker names an OpenAI model as the
proposer, or names none, the job posts that it cannot pick a judge from another family instead of judging. Only
its own earlier verdict for the same head commit stops a repeat; a marker copied by anyone else does not.
