# LastSafe

**The autonomous options expiry airlock. The agent that starts after the trade fills.**

LastSafe monitors defined-risk option verticals as they approach expiration. It maps each
position into possible terminal account states, compares `HOLD`, `CLOSE`, and `ROLL`, and
uses an AI decision layer only after deterministic policy has removed unsafe actions. Orders
are constructed as Alpaca multi-leg paper orders and submitted through the official Alpaca
CLI. Every decision and receipt enters a restart-safe, hash-linked ledger.

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon).

**[Launch the interactive replay demo](https://qybaihe.github.io/lastsafe/)**

![Paper only](https://img.shields.io/badge/execution-paper%20only-367647)
![Python](https://img.shields.io/badge/python-3.11%2B-11130f)
![License](https://img.shields.io/badge/license-MIT-ff6534)

## Why LastSafe

Most trading agents focus on finding and opening a position. The operational risk often starts
later:

- An in-the-money option can be automatically exercised at expiry.
- A short contract can produce an assignment and a 100-share obligation.
- Alpaca may liquidate positions near expiry if buying power cannot support exercise.
- Assignment events are not delivered over Alpaca's trading WebSocket and need REST
  reconciliation.
- A restarted worker must not submit the same closing or rolling order twice.

LastSafe makes the final hours visible and actionable rather than treating expiry as a passive
timestamp.

## Product Flow

1. Read the Alpaca paper account, positions, clock, equity history, option quotes, and chain.
2. Pair supported long/short legs into the nearest-expiry defined-risk vertical.
3. Build three explicit landing states: hold, close, or atomically roll.
4. Apply hard gates for paper mode, options level, market session, quote freshness, defined
   risk, buying power, new-strike clearance, and net roll credit.
5. Give the LLM only the legal action set. It cannot choose symbols, quantity, limit price, or
   account.
6. Submit a two-leg close or four-leg roll through Alpaca CLI with a unique
   `client_order_id`.
7. Persist the snapshot, policy output, model rationale, exact command, and broker receipt in
   a SHA-256-linked SQLite ledger.

## Interface Design

The public interface adapts the warm-cream, dark-green finance language and blurred staggered
entrances of the motion-landing `evergreen-finance` system to an operational expiry runway.
It intentionally avoids the event's crowded neon terminal pattern. All charts and visual
elements are generated locally; there is no stock footage or placeholder product imagery.

## Hackathon Requirements

| Requirement | LastSafe implementation |
| --- | --- |
| Autonomous AI trading agent | Agent selects and executes an expiry action without manual order construction |
| Alpaca Trading API | Account, positions, clock, history, option quotes, chains, and MLeg orders |
| MCP or CLI | Official Alpaca CLI is the required agent tool and the only order write path |
| Options strategy | Vertical spreads with autonomous close and four-leg expiration rolls |
| Paper trading | API host is fixed to `paper-api.alpaca.markets`; CLI receives `ALPACA_LIVE_TRADE=false` |
| P&L evidence | Actual account equity/history in Alpaca mode; replay data is visibly labeled |

## Safety Boundary

The public deployment defaults to `replay` mode. It never sends a broker request. Live-money
execution is not implemented.

- The Trading API base URL is hard-coded to Alpaca paper.
- The CLI environment explicitly sets `ALPACA_LIVE_TRADE=false`.
- Broker credentials remain server-side.
- An LLM can select only an action that deterministic code marked `allowed`.
- Only paired, same-expiration 1:1 verticals are supported.
- Orders use unique client IDs for idempotency and audit correlation.
- Public execution can be protected with `LASTSAFE_EXECUTION_TOKEN`.
- If no later contract passes selection, the agent can close but cannot invent a roll.

## Local Setup

Requirements: Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run uvicorn lastsafe.main:app --reload
```

Open <http://localhost:8000>. Replay mode works without credentials and supports the complete
interactive demo, including a simulated Alpaca CLI payload.

Run quality checks:

```bash
uv run ruff check .
uv run pytest
```

## Alpaca Paper Mode

Install the official Alpaca CLI:

```bash
brew install alpacahq/tap/cli
alpaca version
```

Create a `.env` from `.env.example` and set:

```dotenv
LASTSAFE_MODE=alpaca
LASTSAFE_EXECUTION_ENABLED=false
ALPACA_API_KEY=your_paper_key
ALPACA_SECRET_KEY=your_paper_secret
ALPACA_DATA_FEED=indicative
```

Start with execution disabled. LastSafe will read the account and construct command previews,
but it will not submit orders. Before enabling paper execution, verify all of the following:

- The account is the dedicated hackathon account and began with exactly `$100,000`.
- `options_trading_level` is `3` for multi-leg spreads.
- The account contains one paired vertical supported by LastSafe.
- The official CLI resolves to paper mode with `alpaca doctor`.
- The selected option feed and timestamps are shown correctly.
- A separate development account has completed a submit/cancel/fill smoke test.

Then configure a private worker:

```dotenv
LASTSAFE_EXECUTION_ENABLED=true
LASTSAFE_EXECUTION_TOKEN=a-long-random-token
```

The UI does not collect this token. Keep the public demo in replay/read-only mode and invoke
the protected run endpoint from an authenticated operator or scheduled worker.

## Optional AI Provider

LastSafe accepts any OpenAI-compatible chat-completions endpoint. The event provides optional
Featherless credits, so `.env.example` defaults to its endpoint and suggested model.

```dotenv
LLM_API_KEY=your_key
LLM_BASE_URL=https://api.featherless.ai/v1
LLM_MODEL=zai-org/GLM-5.2
```

Without an API key, the app uses a transparent deterministic fallback. If a model returns a
blocked action or malformed output, the fallback takes over and logs a policy override.

## API

| Route | Purpose |
| --- | --- |
| `GET /health` | Deployment and paper-lock health |
| `GET /api/bootstrap` | Current snapshot and scenario evaluation |
| `POST /api/runs` | Run the agent and optionally request a paper order |
| `GET /api/runs` | Read the append-only decision ledger |
| `GET /api/runs/latest` | Latest agent decision and receipt |
| `GET /api/docs` | OpenAPI explorer |

Example replay run:

```bash
curl -X POST http://localhost:8000/api/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "scenario": {
      "spot_shift_pct": 0,
      "buying_power_pct": 100,
      "minutes_to_close": 95,
      "as_of_date": "2026-09-04"
    },
    "execute": true
  }'
```

## Deployment

The repository includes `Dockerfile`, `docker-compose.yml`, and `render.yaml`. The Render
blueprint intentionally deploys replay mode with execution disabled. For a real paper worker,
use persistent storage, private networking, managed secrets, and a non-sleeping service.

```bash
docker build -t lastsafe .
docker run --rm -p 8000:8000 lastsafe
```

## Scope

Implemented:

- Bull put and bear call credit vertical detection.
- Expiry terminal-state payoff mapping.
- Scenario controls for spot, buying-power reserve, and time to close.
- Deterministic `HOLD`, `CLOSE`, and `ROLL` eligibility.
- Later-expiry chain selection with quote, width, clearance, and credit filters.
- Alpaca CLI two-leg close and four-leg roll payloads.
- Optional constrained LLM decision.
- Replay-safe public dashboard and hash-linked run ledger.

Deliberately out of scope:

- Live-money trading.
- Naked options, calendars, ratio spreads, adjusted contracts, exercise, or DNE instructions.
- General entry-signal generation or claims of profitable alpha.
- 0DTE entries or holding a position through expiration.
- Public redistribution of raw Alpaca market data.

## Disclosures

This prototype is for educational and informational purposes only and is not investment
advice. Paper trading is a simulation; results are hypothetical and do not guarantee future
performance. Options involve substantial risk. Review Alpaca's
[Disclosure Library](https://alpaca.markets/disclosures) and the OCC's
[Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document).
