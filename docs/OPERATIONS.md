# Competition Operations Runbook

## Before Using the Competition Account

1. Register every team member on lablab.ai and create/join the event team.
2. Build and test with a separate development paper account.
3. Create a new dedicated paper account for judging.
4. Before any order, set `LASTSAFE_EXPECTED_ACCOUNT_ID` and run
   `uv run --env-file .env lastsafe-worker --enroll-account`. This binds the database only if cash and equity are
   exactly `$100,000`, options level is 3, and positions/open orders are both empty.
5. Preserve the generated preflight hash and account ID in the private submission checklist.
   Never commit API keys.
6. Confirm `options_trading_level == 3` and account trading is not blocked.
7. Install Alpaca CLI `v0.0.14` or pin the tested event version.
8. Run `alpaca doctor` and independently confirm paper endpoints.

## Development Smoke Test

Use the development account, not the competition account:

1. Query account, clock, option contracts, chain, and indicative quotes.
2. Submit a one-unit defined-risk vertical with a unique client order ID.
3. Verify `new`, `fill` or `partial_fill`, and cancel/close lifecycle.
4. Restart the worker and reconcile by client ID before any retry.
5. Verify a malformed, stale, over-budget, live-mode, and duplicate order is rejected.

## Competition Run

1. Start the account flat. LastSafe may open only one SPY canary after its trend, SMA20,
   quote, delta, DTE, clearance and 0.5% risk gates all pass.
2. Run LastSafe first with execution disabled and inspect the exact command.
3. Enable paper execution only on a private worker protected by a long token.
4. Capture account response, option quotes with timestamps, agent decision, CLI output, order
   response, and resulting positions.
5. Record a clean screen capture while US options are open. Do this no later than Thursday,
   September 3; the event closes Friday at 11:00 ET.
6. Disable execution after evidence is captured. Keep the public URL in replay mode.

## Failure Handling

- Timeout or 5xx after submit: do not retry; query by `client_order_id` first.
- Stale/missing quote: block all writes.
- No valid roll: close or hold according to policy; never synthesize a contract.
- Market closed: do not claim execution; use the timestamped replay.
- Worker restart: query Alpaca positions and open orders before scheduling another cycle.
- API incident: record it, show the fallback, and check <https://status.alpaca.markets/>.
- LLM failure: deterministic policy remains active and logs its source.

## Submission Day

- Export `TECHNICAL_WRITEUP.md` to a one-page PDF.
- Export the pitch deck to PDF.
- Upload a 16:9 cover image and a video no longer than five minutes.
- Verify repository, demo, video, and deck links in an incognito browser.
- Confirm the submitted Alpaca account ID is correct.
- Submit before `2026-09-04T15:00:00Z`; do not depend on manual grace periods.

Current public artifacts:

- Repository: <https://github.com/qybaihe/lastsafe>
- Interactive replay: <https://qybaihe.github.io/lastsafe/>
- CI and image build: <https://github.com/qybaihe/lastsafe/actions>
