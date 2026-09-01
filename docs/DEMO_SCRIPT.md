# Demo Script

Target length: 2 minutes 30 seconds. Hard ceiling: 5 minutes.

## 0:00-0:20 - Problem

"Most trading agents celebrate when an order fills. That is exactly where LastSafe starts.
An expiring short option can become a 100-share assignment, exercise can consume buying
power, and assignment is not even delivered through Alpaca's trading WebSocket. LastSafe is
an autonomous expiry airlock."

Show the hero, paper/replay label, dedicated account equity, and expiry countdown.

## 0:20-0:50 - Terminal States

"This is one SPY put-credit vertical approaching expiration. LastSafe does not show one vague
risk score. It maps the actual landing states below the long strike, at the short strike, and
clear of the spread."

Move **Spot displacement** downward until the short-strike clearance turns negative. Point to
the payoff line and assignment notional.

## 0:50-1:20 - Legal Action Set

"The agent has only three verbs: hold, close, or roll. Deterministic code decides what is
legal. Inside the two-hour airlock, hold is blocked. If the later spread lacks clearance or
credit, roll is blocked too. The language model cannot invent a fourth action or change a
contract."

Reduce buying power, then restore it. Show cards changing from `ROLL` to `CLOSE`.

## 1:20-1:50 - Autonomous Decision

Reset the scenario. Enable **Simulate paper order** and click **Run Airlock**.

"Now the constrained model sees only the cleared actions and evidence. It chooses the landing,
while deterministic code owns quantity, symbols, pricing, and account. If the model fails, the
fallback still runs and that fact is visible."

Show action, confidence, and rationale.

## 1:50-2:15 - Alpaca Execution

"A roll is not four loose orders. LastSafe builds one Alpaca MLeg command: two closing legs
and two opening legs, all carrying explicit position intents and an idempotent client order ID.
The public demo simulates this exact paper payload. This captured run shows the real paper
receipt and account ID."

For the final recording, cut to the genuine competition account receipt and Alpaca order
history. Never call replay data live.

## 2:15-2:30 - Evidence

"Alpaca remains the source of truth. LastSafe stores each snapshot, decision, exact command,
and broker result in a hash-linked restart-safe ledger. It is not another entry scanner. It is
the agent that gets an options position safely home."

End on the ledger and the tagline.
