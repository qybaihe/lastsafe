from datetime import date

import pytest

from lastsafe.engine import evaluate, parse_occ_symbol
from lastsafe.models import (
    Action,
    EntryCandidate,
    ScenarioRequest,
)
from lastsafe.replay import load_replay_snapshot


def test_parse_occ_symbol() -> None:
    contract = parse_occ_symbol("SPY260904P00640000")

    assert contract.underlying == "SPY"
    assert contract.expiration == date(2026, 9, 4)
    assert contract.option_type == "put"
    assert contract.strike == 640


def test_parse_occ_symbol_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        parse_occ_symbol("SPY-PUT-640")


def test_airlock_rolls_near_short_strike() -> None:
    snapshot = load_replay_snapshot()
    result = evaluate(
        snapshot,
        ScenarioRequest(
            spot_shift_pct=0,
            buying_power_pct=100,
            minutes_to_close=95,
            as_of_date=date(2026, 9, 4),
        ),
    )

    assert result.dte == 0
    assert result.urgency == "critical"
    assert result.policy_action == Action.ROLL
    assert result.max_loss == 358
    assert result.roll_net_credit == pytest.approx(0.25)
    hold = next(outcome for outcome in result.outcomes if outcome.action == Action.HOLD)
    roll = next(outcome for outcome in result.outcomes if outcome.action == Action.ROLL)
    assert hold.allowed is False
    assert roll.allowed is True


def test_airlock_closes_when_roll_loses_clearance() -> None:
    snapshot = load_replay_snapshot()
    result = evaluate(
        snapshot,
        ScenarioRequest(
            spot_shift_pct=-2,
            buying_power_pct=100,
            minutes_to_close=45,
            as_of_date=date(2026, 9, 4),
        ),
    )

    roll = next(outcome for outcome in result.outcomes if outcome.action == Action.ROLL)
    assert result.policy_action == Action.CLOSE
    assert roll.allowed is False
    assert any("clearance" in blocker for blocker in roll.blockers)


def test_airlock_holds_before_expiry_window() -> None:
    snapshot = load_replay_snapshot()
    result = evaluate(
        snapshot,
        ScenarioRequest(
            spot_shift_pct=1,
            buying_power_pct=100,
            minutes_to_close=180,
            as_of_date=date(2026, 9, 3),
        ),
    )

    assert result.dte == 1
    assert result.policy_action == Action.HOLD
    assert result.urgency == "watch"


def test_buying_power_gate_forces_close() -> None:
    snapshot = load_replay_snapshot()
    result = evaluate(
        snapshot,
        ScenarioRequest(
            buying_power_pct=0,
            minutes_to_close=95,
            as_of_date=date(2026, 9, 4),
        ),
    )

    assert result.policy_action == Action.STAND_DOWN
    assert next(gate for gate in result.gates if gate.key == "buying-power").passed is False
    close = next(outcome for outcome in result.outcomes if outcome.action == Action.CLOSE)
    assert close.allowed is False
    assert any("close debit" in blocker for blocker in close.blockers)


def test_non_competition_starting_balance_blocks_order_actions() -> None:
    snapshot = load_replay_snapshot()
    snapshot.account.starting_equity = 50_000
    result = evaluate(
        snapshot,
        ScenarioRequest(as_of_date=date(2026, 9, 4)),
    )

    competition_gate = next(
        gate for gate in result.gates if gate.key == "competition-account"
    )
    assert competition_gate.passed is False
    hold = next(outcome for outcome in result.outcomes if outcome.action == Action.HOLD)
    close = next(outcome for outcome in result.outcomes if outcome.action == Action.CLOSE)
    roll = next(outcome for outcome in result.outcomes if outcome.action == Action.ROLL)
    assert hold.allowed is False
    assert roll.allowed is False
    assert close.allowed is True  # Safety exits remain available on a misconfigured account.


def test_stale_roll_quotes_force_close() -> None:
    snapshot = load_replay_snapshot()
    assert snapshot.roll_candidate is not None
    snapshot.roll_candidate.short_leg.quote.timestamp = snapshot.position.opened_at
    result = evaluate(
        snapshot,
        ScenarioRequest(as_of_date=date(2026, 9, 4)),
    )

    roll = next(outcome for outcome in result.outcomes if outcome.action == Action.ROLL)
    assert roll.allowed is False
    assert result.policy_action == Action.CLOSE


def test_expiry_cutoff_stands_down_when_orders_are_no_longer_safe() -> None:
    snapshot = load_replay_snapshot()
    result = evaluate(
        snapshot,
        ScenarioRequest(minutes_to_close=20, as_of_date=date(2026, 9, 4)),
    )

    assert result.policy_action == Action.STAND_DOWN
    assert next(
        outcome for outcome in result.outcomes if outcome.action == Action.STAND_DOWN
    ).allowed


def test_bear_call_terminal_map_has_loss_above_long_strike() -> None:
    snapshot = load_replay_snapshot()
    position = snapshot.position
    assert position is not None
    position.strategy = "bear_call_credit"
    position.long_leg.option_type = "call"
    position.short_leg.option_type = "call"
    position.long_leg.strike = 645
    position.short_leg.strike = 640
    position.long_leg.symbol = "SPY260904C00645000"
    position.short_leg.symbol = "SPY260904C00640000"
    snapshot.roll_candidate = None

    result = evaluate(snapshot, ScenarioRequest(as_of_date=date(2026, 9, 4)))
    hold = next(outcome for outcome in result.outcomes if outcome.action == Action.HOLD)

    assert hold.terminal_states[0].label == "clear"
    assert hold.terminal_states[-1].label == "risk-off"
    assert hold.terminal_states[-1].pnl == -358


def test_flat_account_opens_qualified_canary() -> None:
    snapshot = load_replay_snapshot()
    original = snapshot.position
    assert original is not None
    snapshot.position = None
    snapshot.roll_candidate = None
    snapshot.entry_candidate = EntryCandidate(
        id="entry-canary",
        underlying="SPY",
        strategy="bull_put_credit",
        quantity=1,
        open_credit=1.42,
        max_loss=358,
        dte=4,
        short_clearance_pct=1.5,
        trend_5d_pct=0.8,
        price_vs_sma20_pct=1.2,
        rationale="SPY trend and SMA agree.",
        long_leg=original.long_leg,
        short_leg=original.short_leg,
    )
    snapshot.entry_candidate.long_leg.expiration = date(2026, 9, 8)
    snapshot.entry_candidate.short_leg.expiration = date(2026, 9, 8)
    snapshot.entry_candidate.long_leg.symbol = "SPY260908P00635000"
    snapshot.entry_candidate.short_leg.symbol = "SPY260908P00640000"

    result = evaluate(snapshot, ScenarioRequest(as_of_date=date(2026, 9, 4)))

    assert result.lifecycle_state == "flat"
    assert result.policy_action == Action.OPEN
    assert next(outcome for outcome in result.outcomes if outcome.action == Action.OPEN).allowed


def test_flat_account_rejects_stale_canary_quotes() -> None:
    snapshot = load_replay_snapshot()
    original = snapshot.position
    assert original is not None
    snapshot.position = None
    snapshot.roll_candidate = None
    original.long_leg.quote.timestamp = original.opened_at
    original.short_leg.quote.timestamp = original.opened_at
    snapshot.entry_candidate = EntryCandidate(
        id="entry-stale",
        underlying="SPY",
        strategy="bull_put_credit",
        quantity=1,
        open_credit=1.42,
        max_loss=358,
        dte=4,
        short_clearance_pct=1.5,
        trend_5d_pct=0.8,
        price_vs_sma20_pct=1.2,
        rationale="qualified except stale",
        long_leg=original.long_leg,
        short_leg=original.short_leg,
    )

    result = evaluate(snapshot, ScenarioRequest(as_of_date=date(2026, 9, 4)))

    assert result.policy_action == Action.STAND_DOWN


def test_flat_account_stands_down_without_candidate() -> None:
    snapshot = load_replay_snapshot()
    snapshot.position = None
    snapshot.roll_candidate = None
    snapshot.entry_candidate = None

    result = evaluate(snapshot, ScenarioRequest(as_of_date=date(2026, 9, 4)))

    assert result.policy_action == Action.STAND_DOWN


def test_unsupported_portfolio_blocks_entry() -> None:
    snapshot = load_replay_snapshot()
    snapshot.position = None
    snapshot.roll_candidate = None
    snapshot.portfolio_issues = ["Unmatched option quantities"]

    result = evaluate(snapshot, ScenarioRequest(as_of_date=date(2026, 9, 4)))

    assert result.lifecycle_state == "unsupported"
    assert result.policy_action == Action.STAND_DOWN


def test_flat_canary_builds_open_cli_payload() -> None:
    snapshot = load_replay_snapshot()
    original = snapshot.position
    assert original is not None
    snapshot.position = None
    snapshot.roll_candidate = None
    snapshot.entry_candidate = EntryCandidate(
        id="entry-cli",
        underlying="SPY",
        strategy="bull_put_credit",
        quantity=1,
        open_credit=1.42,
        max_loss=358,
        dte=4,
        short_clearance_pct=1.5,
        trend_5d_pct=0.8,
        price_vs_sma20_pct=1.2,
        rationale="qualified",
        long_leg=original.long_leg,
        short_leg=original.short_leg,
    )

    result = evaluate(snapshot, ScenarioRequest(as_of_date=date(2026, 9, 4)))

    assert result.policy_action == Action.OPEN
