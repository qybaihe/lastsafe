import json
import shlex
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from lastsafe.alpaca import AlpacaClient, AlpacaError
from lastsafe.config import Settings
from lastsafe.models import EntryCandidate, SpreadPosition
from lastsafe.replay import load_replay_snapshot


def test_close_cli_payload_is_a_two_leg_paper_order() -> None:
    client = AlpacaClient(Settings())
    args = client._cli_args("CLOSE", load_replay_snapshot(), "lastsafe-test-close")

    assert args[:3] == ["alpaca", "order", "submit"]
    assert args[args.index("--limit-price") + 1] == "1.60"
    assert args[args.index("--client-order-id") + 1] == "lastsafe-test-close"
    legs = json.loads(args[args.index("--legs") + 1])
    assert len(legs) == 2
    assert [leg["position_intent"] for leg in legs] == ["buy_to_close", "sell_to_close"]


def test_roll_cli_payload_is_an_atomic_four_leg_order() -> None:
    client = AlpacaClient(Settings())
    args = client._cli_args("ROLL", load_replay_snapshot(), "lastsafe-test-roll")

    assert args[args.index("--limit-price") + 1] == "-0.25"
    legs = json.loads(args[args.index("--legs") + 1])
    assert len(legs) == 4
    assert [leg["position_intent"] for leg in legs] == [
        "buy_to_close",
        "sell_to_close",
        "buy_to_open",
        "sell_to_open",
    ]


def test_command_preview_shell_quotes_leg_json() -> None:
    client = AlpacaClient(Settings())
    preview = client.command_preview("ROLL", load_replay_snapshot(), "lastsafe-test-roll")

    assert shlex.split(preview)[0:3] == ["alpaca", "order", "submit"]
    assert "ALPACA_SECRET_KEY" not in preview


def test_parse_nanosecond_alpaca_timestamp() -> None:
    parsed = AlpacaClient._parse_timestamp("2026-09-01T14:32:10.123456789Z")

    assert parsed == datetime(2026, 9, 1, 14, 32, 10, 123456, tzinfo=UTC)


def test_client_order_id_is_stable_for_same_expiry_incident() -> None:
    client = AlpacaClient(Settings())
    snapshot = load_replay_snapshot()

    first = client.client_order_id("ROLL", snapshot)
    second = client.client_order_id("ROLL", snapshot.model_copy(deep=True))

    assert first == second
    assert first.startswith("lastsafe-roll-")
    assert len(first) <= 128


def test_retry_attempt_changes_client_order_id() -> None:
    client = AlpacaClient(Settings())
    snapshot = load_replay_snapshot()

    assert client.client_order_id("ROLL", snapshot, attempt=1) != client.client_order_id(
        "ROLL", snapshot, attempt=2
    )


def test_position_pairing_rejects_residual_naked_quantity() -> None:
    client = AlpacaClient(Settings())
    positions = [
        {
            "symbol": "SPY260904P00635000",
            "qty": "1",
            "avg_entry_price": "1.18",
        },
        {
            "symbol": "SPY260904P00640000",
            "qty": "-2",
            "avg_entry_price": "2.60",
        },
    ]

    spread, issues = client._select_vertical(positions)

    assert spread is None
    assert issues == ["Unmatched option quantities: long 1, short 2"]


def test_position_pairing_supports_flat_account() -> None:
    spread, issues = AlpacaClient(Settings())._select_vertical([])

    assert spread is None
    assert issues == []


def test_spread_model_rejects_credit_larger_than_width() -> None:
    payload = load_replay_snapshot().position.model_dump()
    payload["entry_credit"] = 6

    with pytest.raises(ValidationError):
        SpreadPosition.model_validate(payload)


@pytest.mark.asyncio
async def test_competition_preflight_requires_pristine_100k_account() -> None:
    client = AlpacaClient(Settings())
    responses = {
        "/v2/account": {
            "id": "new-account",
            "status": "ACTIVE",
            "equity": "100000",
            "cash": "100000",
            "options_trading_level": 3,
        },
        "/v2/positions": [],
        "/v2/orders": [],
    }

    async def get(url, *, params=None):
        return next(value for key, value in responses.items() if url.endswith(key))

    client._get = get
    result = await client.competition_preflight()

    assert result["account_id"] == "new-account"
    assert result["equity"] == 100_000
    assert len(result["preflight_hash"]) == 64


@pytest.mark.asyncio
async def test_competition_preflight_rejects_existing_positions() -> None:
    client = AlpacaClient(Settings())

    async def get(url, *, params=None):
        if url.endswith("/v2/account"):
            return {
                "id": "reused-account",
                "status": "ACTIVE",
                "equity": "100000",
                "cash": "100000",
                "options_trading_level": 3,
            }
        if url.endswith("/v2/positions"):
            return [{"symbol": "SPY", "qty": "1"}]
        return []

    client._get = get

    with pytest.raises(AlpacaError, match="zero positions"):
        await client.competition_preflight()


def test_open_cli_payload_uses_credit_sign_and_open_intents() -> None:
    snapshot = load_replay_snapshot()
    position = snapshot.position
    assert position is not None
    snapshot.position = None
    snapshot.roll_candidate = None
    snapshot.entry_candidate = EntryCandidate(
        id="entry-payload",
        underlying="SPY",
        strategy="bull_put_credit",
        quantity=1,
        open_credit=1.42,
        max_loss=358,
        dte=4,
        short_clearance_pct=1.5,
        trend_5d_pct=1,
        price_vs_sma20_pct=1,
        rationale="qualified",
        long_leg=position.long_leg,
        short_leg=position.short_leg,
    )

    args = AlpacaClient(Settings())._cli_args("OPEN", snapshot, "entry-id")
    legs = json.loads(args[args.index("--legs") + 1])

    assert args[args.index("--limit-price") + 1] == "-1.42"
    assert [leg["position_intent"] for leg in legs] == [
        "buy_to_open",
        "sell_to_open",
    ]


def test_standard_contract_deliverable_validation() -> None:
    standard = {
        "underlying_symbol": "SPY",
        "deliverables": [
            {
                "type": "equity",
                "symbol": "SPY",
                "amount": "100",
                "delayed_settlement": False,
            }
        ],
    }
    adjusted = {
        **standard,
        "deliverables": [{**standard["deliverables"][0], "amount": "150"}],
    }

    assert AlpacaClient._has_standard_deliverable(standard) is True
    assert AlpacaClient._has_standard_deliverable(adjusted) is False


def test_cli_error_sanitizer_drops_debug_fields() -> None:
    message = json.dumps(
        {
            "message": "order not found",
            "request_headers": {"APCA-API-SECRET-KEY": "must-not-leak"},
        }
    )

    assert AlpacaClient._sanitize_cli_error(message) == "order not found"
