# Dispatcher

`dispatch.yml` in agent-control runs `python3 -m dispatcher` every 5 minutes and on issue events, under one global
concurrency group, so there is exactly one writer of run state (D4). State lives only in agent-control issues (C3).
It reads repo policies with the gate's parser, so it needs `gate/` on the same kit commit.

## One tick

1. If repo variable `AGENT_KIT_PAUSED` is `true`, stop before any API call.
2. Pull unresolved Sentry issues (`GET /api/0/organizations/{org}/issues/?project=…&statsPeriod=24h`) and Datadog
   Error Tracking issues (`POST /api/v2/error-tracking/issues/search`) for each registered repo. Sentry pages follow
   its `Link` cursor; Datadog returns at most 100 issues with no cursor, so a full window is split in halves. Each
   source makes at most 10 requests per repo per tick.
3. Upsert one issue per fingerprint (`fp` = first 16 hex of sha256 of `<source>:<id>`), labelled `agent:incident` and
   `agent:needs-decision`. 50 replays of one error find the same issue. A closed issue whose error is seen again
   after it closed is reopened (that is what makes a false green visible); an ignored one is not.
4. For each open incident, oldest first, by its markers (and each closed one updated in the last 7 days, whose open
   claim keeps counting against the caps until its run ends; a closed claim is reconciled but never retried):
   - A claim with a run line and no result: read the Cursor run and send pending steers. Finished with a PR →
     result `pr` (shown as `agent:pr-open`). Finished without one → its reply is posted as the triage, result
     `triage`. Error, cancelled or expired → result `failed`.
   - A claim with no run line for 10 min: look the attempt up in Cursor's list (INV-8). Found → record it.
     Not found → result `lost`, then one retry with a new attempt.
   - Otherwise: use the latest valid decision comment from a decider. With none, wait, unless the Mac mini heartbeat
     is stale (C4: older than 30 min or `accepting: false`), then send the default "reproduce, then fix or write triage" run.
5. Before a run starts: 1 active run per repo, 3 globally, and the daily spend cap (kit caps, lowered by the repo's
   policy `caps`, never raised). The claim comment is written **before** Cursor is called, and
   the Cursor agent id is `bc-<attempt>`, so a repeated create returns 409 instead of a second run.
6. After `max_attempts` failed attempts, or when the daily spend cap is hit: a comment, `agent:needs-human`, and an
   assignment to `escalate_to` (GitHub emails the assignee). Nothing writes to Slack (INV-10).
7. An open incident with no active run, no update for 30 days, and no event in this tick's pull is closed with a
   comment. It is not closed when its repo's source could not be read in full (an error, or the 10-request limit),
   and it reopens if an event arrives after that tick's pull began.

Default models come from `models.default`. A decision may name another model; it is used only if it is in
`models.included` or an approver from the repo's policy added `agent:paid-ok` (checked from the issue's events).

## Configuration in agent-control

The dispatcher reads two files from agent-control's `main` checkout:

- `agent-kit/repos.json`: the gate's registry, `{"v": 1, "repos": ["owner/name", ...]}`. Only these repos are read
  or dispatched; any other shape stops the tick.
- `agent-kit/dispatch.json`: dispatcher settings. `sources` maps a registered repo to its Sentry project and Datadog
  query; a repo without an entry gets no intake.

```json
{
  "v": 1,
  "heartbeat_issue": 1,
  "deciders": ["yudduy-mini"],
  "escalate_to": "yudduy",
  "sources": {
    "yudduy/shop": {"sentry": {"org": "acme", "project": "shop"}, "datadog": {"query": "service:shop-api"}}
  },
  "run_cost_usd": 1,
  "models": {"default": "composer-2", "included": ["composer-2"]}
}
```

Fixed in code, not configurable:
- Kit caps: 1 run per repo, 3 globally, $20 a day, 2 attempts. A repo's policy `caps` can lower the first, third and
  fourth, never raise them. The policy is read with the gate's parser (`gate/policy.py`); a missing or invalid policy
  means no run for that repo.
- The dispatcher's identity is `github-actions[bot]`, the author of the workflow token's writes. Claim, run and
  reopen markers, and incident issues, from any other login are ignored.

Spend (phase 1): every claimed attempt costs a flat `run_cost_usd`, because Cursor reports tokens, not dollars.
The daily cap counts claims made since 00:00 UTC across all incidents. Replace it with real cost once Cursor's
usage can be priced.

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
python3 -m dispatcher.macmini steer 12 --text "Also cover the empty-name case"
```

The helper never calls Cursor and needs no Cursor key. `steer` posts a request comment
(`<!-- agent-kit:steer v1 {"id": …, "prompt_sha256": …} -->` and the text), accepted only from a login in
`deciders` with an intact prompt hash. On its next tick the dispatcher sends it into the current claim's active
run. It writes the `steered` marker before calling Cursor and fills in the result after, so a failed write can never
cause a second send; a request id is sent at most once. With no active run, or when its claim never reached Cursor,
the request is refused.
A steer can still become the agent's next run (Cursor queues it, or the turn ends before reading it). That run
belongs to the same claim: the claim stays reserved, counting against the caps, until 10 minutes after the steered
run ends with no later run, and then the claim closes on whichever run came last. If Cursor still starts a run after
that, the run has no open claim and the next tick cancels it, so one fingerprint never keeps more than one active run.

## Cursor

`dispatcher/cursor.py` is the only module that talks to Cursor (Cloud Agents API v1, beta): create, list, get agent,
get run, steer. The dispatcher is its only caller. If the beta changes, only that file changes.

## State and labels

Run state comes only from markers the dispatcher writes (`claim`, `run`, `result`, `escalate`, `reopen`, `idle`,
`steered`), the requests deciders write (`decision`, `steer`), and Cursor's run state. A claim without a `result`
marker is a reservation, whatever the labels say. The dispatcher's labels (`agent:claimed`, `agent:running`,
`agent:pr-open`, `agent:triaged`, `agent:needs-human`, and `agent:incident`) only display that state, and each tick
resets them to match; removing or adding one by hand changes nothing. Incidents are found by listing every issue the
dispatcher opened with an intake marker, not by label. A marker counts only when it opens a dispatcher comment, and
text relayed from Cursor or a source is made inert, so quoted text can never pose as a marker.

Each tick also lists Cursor's running agents (up to 20 pages of 100). If the list is longer, no new run starts that
tick. A running `agent-kit <fp> <attempt>` agent counts against its repo's caps even if its incident was closed long
ago, and its incident is followed until the run ends. A running agent whose attempt is not its incident's open claim
is cancelled; until the cancel succeeds it keeps counting. Occupancy is keyed by attempt, so a claim and its own
agent count once.
