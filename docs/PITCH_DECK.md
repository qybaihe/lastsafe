# Pitch Deck Outline

## Slide 1 - LastSafe

**The autonomous options expiry airlock.**

The agent that starts after the trade fills.

Visual: expiry runway and `HOLD / CLOSE / ROLL`.

## Slide 2 - The Hidden Last-Mile Risk

- ITM contracts can be automatically exercised.
- Short options can create 100-share assignment obligations.
- Buying-power shortfalls can trigger expiry-day liquidation.
- Assignment is not streamed over the trading WebSocket.
- Most agents optimize entry and treat expiry as a date field.

## Slide 3 - One Position, Three Landing States

Show a vertical spread and three outcomes:

- `HOLD`: terminal payoff and assignment exposure.
- `CLOSE`: known debit and locked P&L.
- `ROLL`: one atomic four-leg move to a later expiration.

## Slide 4 - Separation of Authority

- Alpaca state creates the facts.
- Deterministic engine creates the legal action set.
- AI chooses only among legal actions and explains why.
- Alpaca CLI submits the exact paper MLeg order.
- Broker reconciliation and hash-linked ledger close the loop.

## Slide 5 - Hard Risk Gates

Paper endpoint, Level 3, market open, quote freshness, paired vertical, max loss below 1%,
buying-power reserve, later-expiry clearance, minimum net credit, and idempotent client IDs.

## Slide 6 - Live Product

Use one large screenshot of the expiry runway. Annotate:

- Terminal P&L map.
- Incident controls.
- Legal action cards.
- Exact CLI receipt.
- Restart-safe ledger.

## Slide 7 - Alpaca-Native Implementation

- Trading API: account, positions, clock, history, orders.
- Market Data API: option quotes and chains.
- Alpaca CLI: paper-only two-leg close and four-leg roll.
- Alpaca CLI: machine-readable agent tool, order execution, and reconciliation by client ID.
- Dedicated fresh `$100,000` paper account.
- Private autonomous worker: scheduled cycles, terminal-order monitoring, position verification.
- Evidence packet: fill IDs, attributed P&L, run hash, and Airlock Value counterfactual.

## Slide 8 - Testable Result

Fill with real competition-account data:

- Paper P&L: `[TODO]`
- Maximum drawdown: `[TODO]`
- Options orders submitted/filled: `[TODO]`
- Expiry incidents managed: `[TODO]`
- Duplicate submissions: `0`
- Replay and live evidence clearly separated.

Do not present the contest result as proof of future alpha.

## Slide 9 - Why It Wins

- Distinct white space: post-fill lifecycle rather than another scanner.
- Visible autonomous behavior in a two-minute demo.
- Meaningful use of Trading API and CLI.
- Options are fundamental, not decorative.
- Safety is demonstrated through changing legal actions, not asserted in prose.

## Slide 10 - LastSafe

**Entries find trades. LastSafe gets them home.**

Repository, demo URL, QR code, team names, and disclosures.
