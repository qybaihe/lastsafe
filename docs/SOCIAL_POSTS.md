# Build in Public Posts

Publish on X or LinkedIn during the event. Tag both `@lablabai` / lablab.ai and
`@AlpacaHQ` / Alpaca. Replace bracketed evidence only after it is real. Do not describe replay
orders or replay P&L as live.

## Post 1 - The Problem

Most AI trading agents stop when an order fills. We are starting there.

An expiring option can become an unexpected exercise, assignment, or 100-share obligation,
and assignment is not delivered through Alpaca's trading WebSocket. We are building LastSafe:
an autonomous options expiry airlock that chooses HOLD, CLOSE, or ROLL before time chooses for
you.

Built for the Alpaca AI Trading Agents Hackathon by @lablabai and @AlpacaHQ.

#AITrading #Options #BuildInPublic #AlpacaAPI

## Post 2 - The Product

The first LastSafe expiry runway is online.

Move SPY through the short strike, consume the account's buying-power reserve, and advance the
clock. The legal action set changes in real time. The model never invents symbols or position
size; deterministic policy clears the actions first.

Interactive replay: https://qybaihe.github.io/lastsafe/

The replay is clearly labeled and sends no broker orders.

@lablabai @AlpacaHQ #OptionsTrading #AIAgents #BuildInPublic

## Post 3 - A Failure We Fixed

Today's most important LastSafe fix was not visual.

Our first execution design generated a random client order ID on every run. That looked
idempotent, but a restart could still create a duplicate order. We replaced it with a stable ID
derived from account + expiry incident + action + roll contracts, and now query Alpaca by that
ID before every submit and reconcile again afterward.

The broker is truth. The ledger remembers.

Code: https://github.com/qybaihe/lastsafe

@lablabai @AlpacaHQ #Engineering #FinTech #BuildInPublic

## Post 4 - Alpaca-Native Execution

LastSafe now constructs both expiry exits through Alpaca's official CLI:

- CLOSE: one two-leg MLeg limit order
- ROLL: one atomic four-leg MLeg order
- Explicit buy/sell-to-close and buy/sell-to-open intents
- Signed net pricing: positive debit, negative credit
- Stable client order IDs and pre/post-submit reconciliation
- Hard paper endpoint lock

The public demo shows the exact shell-safe command while keeping credentials and execution
private.

@lablabai @AlpacaHQ #AlpacaAPI #Options #AIAgents

## Post 5 - Final Evidence

LastSafe is submitted: the AI agent that starts after the trade fills.

Competition evidence:

- Paper account began at exactly `$100,000`
- Options P&L: `[REAL RESULT]`
- Maximum drawdown: `[REAL RESULT]`
- Expiry actions completed: `[REAL RESULT]`
- Duplicate submissions: `0`

We do not claim that four days prove alpha. We demonstrate something narrower and testable:
an options position can reach expiry with autonomous decisions, bounded authority, exact Alpaca
execution, and a replayable audit trail.

Demo: https://qybaihe.github.io/lastsafe/
Code: https://github.com/qybaihe/lastsafe

@lablabai @AlpacaHQ #BuildInPublic #AITrading #Hackathon
