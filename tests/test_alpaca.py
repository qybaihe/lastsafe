import json
import shlex
from datetime import UTC, datetime

from lastsafe.alpaca import AlpacaClient
from lastsafe.config import Settings
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
