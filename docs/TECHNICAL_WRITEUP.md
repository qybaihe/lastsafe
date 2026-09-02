# LastSafe Technical Write-Up

## The Agent That Starts After the Trade Fills

LastSafe is an autonomous expiry-operations agent for option verticals in an Alpaca paper
account. Instead of competing with entry scanners, it manages the point where a profitable,
defined-risk trade can turn into an unwanted exercise, assignment, liquidation, or capital
obligation. For every supported spread it creates three concrete landing states: hold through
expiry, buy the spread back, or close and reopen it at a later expiration in one four-leg MLeg
order.

When the dedicated account is flat, a narrow lifecycle canary may open one SPY credit
vertical. This is not a general scanner: direction requires five-session trend and SMA20
agreement, and contract code then enforces 2-7 DTE, 0.15-0.28 short delta, at least 1% strike
clearance, two-sided quotes, bid/ask width below 25% of mid, credit above 15% of spread width,
and maximum loss below both `$500` and 0.5% of equity.

## AI Logic

The AI layer is intentionally narrow. It receives the account's paper flag, equity and options
buying power; the spread type; current spot; days and minutes to expiry; short-strike clearance;
conservative close debit; available roll credit; and the deterministic result for each action.
It must return one JSON action (`HOLD`, `CLOSE`, or `ROLL`), confidence, short thesis, cited
facts, and reasons for rejecting the alternatives. It never receives authority to invent a
contract, quantity, limit price, endpoint, or account.

If the model is unavailable, malformed, or chooses a blocked action, LastSafe runs the same
workflow with a visible deterministic fallback. This makes autonomy independent of model
uptime while preserving an AI judgment layer for ambiguous legal choices.

## Risk Gates

Deterministic code has final authority. It verifies:

1. The account is paper-only and the API URL is Alpaca's paper endpoint.
2. The account has effective options trading level 3 before any MLeg action.
3. The regular options session is open.
4. Both legs have recent, non-crossed, two-sided quotes.
5. The detected position is exactly one paired 1:1 same-expiration vertical with no residual
   or unexplained option legs.
6. A new canary's maximum spread loss is no more than 0.5% of current equity; an existing
   campaign remains bounded below 1%.
7. Options buying power exceeds the defined-risk reserve.
8. A roll uses the same width, later expiration, standard long protection, at least 0.75%
   short-strike clearance, and at least `$0.05` conservative net credit after closing the old
   spread.
9. Expiry-window holds are blocked when the short option is in or near the money, the clock is
   inside the two-hour airlock, or post-expiry notional exceeds the reserve policy.
10. The exact competition account identity and pristine `$100,000` starting snapshot must have
    been bound before execution; the account is not inferred from a rolling history window.

No roll candidate means no roll action. The agent falls back to close rather than synthesizing
symbols. `client_order_id` makes every submission idempotent and correlates Alpaca, CLI, and
local records.

## Alpaca Infrastructure

LastSafe reads account state, positions, market clock, portfolio history, option quotes, and
option-chain snapshots from Alpaca's Trading and Market Data APIs. It uses the free
`indicative` option feed explicitly unless an account has OPRA entitlement, and displays the
feed and timestamps instead of presenting indicative quotes as NBBO.

The order path is the official Alpaca CLI. A close is a two-leg MLeg limit order; a roll is a
single four-leg MLeg order containing `buy_to_close`, `sell_to_close`, `buy_to_open`, and
`sell_to_open` intents. The worker always exports `ALPACA_LIVE_TRADE=false`. The public demo
uses a visibly labeled replay and emits the exact shell-safe command without sending it.

Each cycle stores the full normalized snapshot, gate results, model decision, exact order
preview, broker receipt, previous record hash, and current SHA-256 hash in SQLite. On restart,
the system can reconcile broker state by client ID before any retry. Alpaca remains the source
of truth; the local ledger supplies reproducibility and operator evidence.

A long-lived worker repeats this cycle without browser interaction under a SQLite lease. It
polls submitted orders until filled or terminal, cancels orders at timeout, recovers ambiguous
submissions by stable client ID, and verifies the resulting book through Alpaca CLI. A fill is
not treated as completed lifecycle evidence until old legs are gone and expected new legs are
present. The sanitized evidence packet records broker IDs, fill fields, account fingerprint,
P&L attribution, code revision, run hash, and the modeled Airlock counterfactual.

## Strategy and P&L

LastSafe does not claim a predictive edge from a four-day contest. Its testable strategy is to
preserve the economics of an existing defined-risk credit vertical by avoiding unmanaged
expiry outcomes. It estimates close and roll economics using conservative two-sided quotes,
maps payoff at prices below, at, and above the short strike, and reports actual paper-account
equity separately from replay data. The success criteria are an eligible paper options action,
a real Alpaca order lifecycle, no duplicate execution, and transparent P&L and drawdown.
