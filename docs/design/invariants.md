# Invariants: agent kit

Rules that must stay true after every change. Every design and build check loads this file first.
Source: [2026-11-10-agent-kit.md](2026-11-10-agent-kit.md). Changing any invariant needs a new design record that puts it on the card as a one-way door.
All added 2026-11-10, approved by Duy.

## INV-1 Only the gate App unlocks a merge
Rule: Only the agent-gate App's check unlocks a merge; no bypass, admins included.
Why: D2. Scope: every onboarded repo's rulesets.
Check: the ruleset API shows the `agent-gate` required check bound to the App id and an empty bypass list.

## INV-2 The gate key never meets PR code
Rule: The gate App's key lives only in agent-control's protected environment; never on the Mac mini or any machine that runs PR code.
Why: D2. Scope: agent-control, Mac mini, Cursor VMs, verify runners.
Check: the gate job never checks out PR code; a Mac mini secret scan finds no key.

## INV-3 Policy and verification come from main
Rule: Policy, journeys and the verify workflow are read from main, never the PR.
Why: D6, D7. Scope: gate, verify workflow.
Check: a test PR editing policy stays blocked.

## INV-4 Agents and authors never approve
Rule: An agent or the PR author never counts as an approver; approval = a listed human who isn't the author, on this exact commit.
Why: D3. Scope: gate approval rule.
Check: a test PR approved by the bot stays blocked.

## INV-5 No agent identity holds merge power
Rule: No agent identity (Mac mini, Cursor VM, Grok Bot) can merge, approve or change merge rules.
Why: D3. Scope: every token and App an agent can use.
Check: an audit of every agent token's scopes.

## INV-6 Evidence only from the kit's verify run on this commit
Rule: The gate accepts evidence only from the kit's verify workflow run on the exact commit under review.
Why: D6. Scope: gate, `evidence.json`.
Check: a test PR with a hand-edited evidence file fails.

## INV-7 No production credentials where agents or verifiers run
Rule: No production credential exists on a Cursor machine, a verify runner or the Mac mini.
Why: D9. Scope: Cursor environments and secrets, verify runners, Mac mini.
Check: the `agent-kit doctor` scan.

## INV-8 One active fix run per error
Rule: Each error has at most one active fix run.
Why: D4. Scope: dispatcher.
Check: replaying one error 50× starts 1 run.

## INV-9 Sensitive classes never auto-merge
Rule: Auth, payments, migrations, tenancy, destructive operations, and policy/CI/verify files never auto-merge; repo policy can only add to this list.
Why: D7. Scope: gate classifier, kit floor.
Check: gate unit tests.

## INV-10 Agents never write to Slack
Rule: Agents never write to Slack.
Why: D4 escalation, Duy's standing rule. Scope: the whole kit.
Check: the kit contains no Slack token or integration.

## INV-11 Learning takes effect only through approved PRs
Rule: Anything the system learns takes effect only through a human-approved PR.
Why: D10. Scope: kit, skills repo, agent memories.
Check: nothing in the kit or skills repo writes rules or skills outside a PR.

## INV-12 Learning agents only add test cases
Rule: A learning agent may only add test cases; changing existing expectations, graders, checker thresholds, policy, the gate, or verification pass criteria needs a human.
Why: D10. Scope: learning PRs.
Check: a CI path check fails learning PRs that touch those files.

## INV-13 Every improvement is proven on a real failure
Rule: Every accepted improvement has a case from a real failure that fails before and passes after, with the full suite passing.
Why: D10, I18. Scope: learning PRs.
Check: the PR's CI output shows both runs.
