# Journeys

Each file here is a Playwright spec for one thing a user does in {{APP}}. Its entry under `journeys:` in
`../policy.yml` lists the code areas it covers and at least one `data_assertions` query that checks the stored
result, not just the screen. The kit's verify workflow runs these from the base branch on every PR.
Changing a journey is a governance change and needs a human approval.
