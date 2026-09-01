# LastSafe Technical Write-Up

## The Agent That Starts After the Trade Fills

LastSafe is an autonomous expiry-operations agent for option verticals in an Alpaca paper
account. Instead of competing with entry scanners, it manages the point where a profitable,
defined-risk trade can turn into an unwanted exercise, assignment, liquidation, or capital
obligation. For every supported spread it creates three concrete landing states: hold through
expiry, buy the spread back, or close and reopen it at a later expiration in one four-leg MLeg
order.

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
5. The detected position is a paired 1:1 same-expiration vertical with bounded loss.
6. Maximum spread loss is no more than 1% of current equity.
7. Options buying power exceeds the defined-risk reserve.
8. A roll uses the same width, later expiration, standard long protection, at least 0.75%
   short-strike clearance, and at least `$0.05` conservative net credit after closing the old
   spread.
9. Expiry-window holds are blocked when the short option is in or near the money, the clock is
   inside the two-hour airlock, or post-expiry notional exceeds the reserve policy.

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

## Strategy and P&L

LastSafe does not claim a predictive edge from a four-day contest. Its testable strategy is to
preserve the economics of an existing defined-risk credit vertical by avoiding unmanaged
expiry outcomes. It estimates close and roll economics using conservative two-sided quotes,
maps payoff at prices below, at, and above the short strike, and reports actual paper-account
equity separately from replay data. The success criteria are an eligible paper options action,
a real Alpaca order lifecycle, no duplicate execution, and transparent P&L and drawdown.
