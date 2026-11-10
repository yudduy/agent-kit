# Dispatcher

`dispatch.yml` in agent-control runs `python3 -m dispatcher` every 5 minutes and on issue events, under one global
concurrency group, so there is exactly one writer of run state (D4). State lives only in agent-control issues (C3).

## One tick

1. If repo variable `AGENT_KIT_PAUSED` is `true`, stop before any API call.
2. Pull unresolved Sentry issues (`GET /api/0/organizations/{org}/issues/?project=…&statsPeriod=24h`) and Datadog
   Error Tracking issues (`POST /api/v2/error-tracking/issues/search`) for each registered repo.
3. Upsert one issue per fingerprint (`fp` = first 16 hex of sha256 of `<source>:<id>`), labelled `agent:incident` and
   `agent:needs-decision`. 50 replays of one error find the same issue. A closed issue whose error is seen again
   after it closed is reopened (that is what makes a false green visible); an ignored one is not.
4. For each open incident, oldest first:
   - `agent:running`: read the Cursor run. Finished with a PR → `agent:pr-open`. Finished without one → its reply is
     posted as the triage and `agent:triaged`. Error, cancelled or expired → a failed attempt.
   - `agent:claimed` with no run line for 10 min: look the attempt up in Cursor's list (INV-8). Found → record it.
     Not found → one retry with a new attempt.
   - Otherwise: use the latest valid decision comment from a decider. With none, wait, unless the Mac mini heartbeat
     is stale (C4: older than 30 min or `accepting: false`), then send the default "reproduce, then fix or write triage" run.
5. Before a run starts: 1 active run per repo, 3 globally, and the daily spend cap (kit caps, lowered by the repo's
   policy `caps`, never raised). The claim comment and `agent:claimed` are written **before** Cursor is called, and
   the Cursor agent id is `bc-<attempt>`, so a repeated create returns 409 instead of a second run.
6. After `max_attempts` failed attempts, or when the daily spend cap is hit: a comment, `agent:needs-human`, and an
   assignment to `escalate_to` (GitHub emails the assignee). Nothing writes to Slack (INV-10).

Default models come from `models.default`. A decision may name another model; it is used only if it is in
`models.included` or an approver from the repo's policy added `agent:paid-ok` (checked from the issue's events).

## Configuration: `agent-kit.json` in agent-control

```json
{
  "v": 1,
  "control_repo": "yudduy/agent-control",
  "heartbeat_issue": 1,
  "deciders": ["yudduy-mini"],
  "escalate_to": "yudduy",
  "repos": {
    "yudduy/shop": {"branch": "main", "sentry": {"org": "acme", "project": "shop"}, "datadog": {"query": "service:shop-api"}}
  },
  "caps": {"runs_per_repo": 1, "global_runs": 3, "daily_usd": 20, "max_attempts": 2},
  "run_cost_usd": 1,
  "models": {"default": "composer-2", "included": ["composer-2"]},
  "agent_logins": []
}
```

`run_cost_usd` is a flat estimate per run, because Cursor reports tokens, not dollars. `agent_logins` lists the bot
identity that authors agent PRs (O2), for the harvest.

## Secrets (environment `dispatch`)

`CURSOR_API_KEY` (the dispatcher's own key), `SENTRY_TOKEN` (`event:read`), `DATADOG_API_KEY` and `DATADOG_APP_KEY`
(`error_tracking_read`), and `AGENT_KIT_READ_TOKEN` (fine-grained, Contents: read on onboarded repos, to read their
policy). Issue writes use the workflow token (`issues: write`), so the dispatcher's own comments never re-trigger it.
The gate App key is never here.

## Mac mini helper

Run from a kit checkout with a fine-grained token that has only Issues read/write on agent-control:

```sh
export AGENT_KIT_CONTROL_REPO=yudduy/agent-control AGENT_KIT_HEARTBEAT_ISSUE=1 GITHUB_TOKEN=…
python3 -m dispatcher.macmini heartbeat                    # every 5 min; edits its own comment in place
python3 -m dispatcher.macmini pending                      # incidents waiting for a decision
python3 -m dispatcher.macmini decide 12 fix --prompt-file prompt.md
CURSOR_API_KEY=… python3 -m dispatcher.macmini steer 12 --text "Also cover the empty-name case"
```

The helper never creates runs; only the dispatcher does (D5).

## Cursor

`dispatcher/cursor.py` is the only module that talks to Cursor (Cloud Agents API v1, beta): create, list, get agent,
get run, steer. If the beta changes, only that file changes.
