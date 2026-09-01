from datetime import date

import pytest

from lastsafe.engine import evaluate, parse_occ_symbol
from lastsafe.models import Action, ScenarioRequest
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

    assert result.policy_action == Action.CLOSE
    assert next(gate for gate in result.gates if gate.key == "buying-power").passed is False


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
    assert not any(outcome.allowed for outcome in result.outcomes)


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
