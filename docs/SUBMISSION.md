# Submission Copy

## Title

LastSafe: Autonomous Options Expiry Airlock

## Short Description

The AI agent that starts after the trade fills: LastSafe autonomously holds, closes, or
atomically rolls expiring option spreads on Alpaca paper trading before assignment becomes an
accident.

## Long Description

Most AI trading agents stop after they discover and open a trade. LastSafe begins there. It is
an autonomous expiry-operations agent for defined-risk option verticals in an Alpaca paper
account. As a spread approaches expiry, LastSafe maps the position into its possible terminal
account states and compares three legal actions: hold, close, or roll to a later expiration in
one atomic four-leg order.

The product combines an AI judgment layer with deterministic authority. The model can choose
only among actions that already passed hard checks for paper mode, options level, session,
quote freshness, bounded loss, buying power, strike clearance, and conservative roll credit.
It cannot invent contracts, set quantity, change price, or bypass a failed gate. If the model is
unavailable or returns an illegal action, a visible deterministic policy takes over.

LastSafe integrates Alpaca's Trading API and Market Data API for account state, positions,
clock, equity history, option quotes, and chains. It executes two-leg closes and four-leg rolls
through the official Alpaca CLI with idempotent client order IDs. Every snapshot, decision,
command, and receipt enters a SHA-256-linked ledger so a restart cannot silently rewrite the
story.

The public experience includes an interactive expiry runway: judges can move spot through the
short strike, consume buying-power reserve, and advance the clock to see the legal action set
change. Replay data is clearly labeled, while competition P&L and orders come from the
dedicated `$100,000` Alpaca paper account. LastSafe does not claim that a short contest proves
alpha. It demonstrates a narrower and testable value: making the dangerous final hours of an
options trade observable, autonomous, and survivable.

## Suggested Tags

Alpaca, AI Agents, Options, FinTech, Trading API, Alpaca CLI, FastAPI, Featherless, Risk
Management, Web Application

## URLs To Fill

- Public GitHub repository: `[TODO]`
- Online demo: `[TODO]`
- Video presentation: `[TODO]`
- Slide presentation: `[TODO]`
- Alpaca paper account ID: `[TODO - submit privately in the required field]`
- Technical one-pager: `docs/TECHNICAL_WRITEUP.md` or exported PDF
- Social post 1: `[TODO]`
- Social post 2: `[TODO]`
- Social post 3: `[TODO]`
- Social post 4: `[TODO]`
- Social post 5: `[TODO]`

## Submission Checklist

- [ ] Every team member is registered and approved on lablab.ai.
- [ ] Team has 1-6 members and the project is under Options Alpha Agents.
- [ ] Public repository is MIT licensed and contains no secrets.
- [ ] Dedicated fresh Alpaca paper account started at exactly `$100,000`.
- [ ] Submitted account ID matches the account used by the agent.
- [ ] At least one genuine options order lifecycle is captured on that account.
- [ ] Demo URL is public, stable, and defaults to labeled replay/read-only mode.
- [ ] Video is no more than five minutes.
- [ ] Pitch deck is exported to PDF.
- [ ] One-page AI logic, risk gate, and infrastructure write-up is attached/exported.
- [ ] Cover image is 16:9 PNG or JPG.
- [ ] All replay, indicative-feed, paper-trading, and risk disclosures are visible.
- [ ] Submission is complete before September 4, 2026, 15:00 UTC.
