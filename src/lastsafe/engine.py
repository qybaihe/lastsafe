from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite

from .models import (
    Action,
    ActionOutcome,
    DeskSnapshot,
    Evaluation,
    GateResult,
    ScenarioRequest,
    TerminalState,
)

OCC_PATTERN = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True, slots=True)
class ParsedOptionSymbol:
    underlying: str
    expiration: date
    option_type: str
    strike: float


def parse_occ_symbol(symbol: str) -> ParsedOptionSymbol:
    match = OCC_PATTERN.fullmatch(symbol.strip().upper())
    if not match:
        raise ValueError(f"Unsupported OCC option symbol: {symbol}")
    expiration = datetime.strptime(match.group(2), "%y%m%d").date()
    return ParsedOptionSymbol(
        underlying=match.group(1),
        expiration=expiration,
        option_type="call" if match.group(3) == "C" else "put",
        strike=int(match.group(4)) / 1000,
    )


def _spread_width(snapshot: DeskSnapshot) -> float:
    position = snapshot.position
    return abs(position.short_leg.strike - position.long_leg.strike)


def _payoff(snapshot: DeskSnapshot, terminal_spot: float) -> float:
    position = snapshot.position
    quantity = position.quantity
    if position.strategy == "bull_put_credit":
        short_intrinsic = max(position.short_leg.strike - terminal_spot, 0)
        long_intrinsic = max(position.long_leg.strike - terminal_spot, 0)
    else:
        short_intrinsic = max(terminal_spot - position.short_leg.strike, 0)
        long_intrinsic = max(terminal_spot - position.long_leg.strike, 0)
    return round((position.entry_credit - short_intrinsic + long_intrinsic) * 100 * quantity, 2)


def _short_distance_pct(snapshot: DeskSnapshot, spot: float) -> float:
    short_strike = snapshot.position.short_leg.strike
    if snapshot.position.strategy == "bull_put_credit":
        return (spot - short_strike) / spot * 100
    return (short_strike - spot) / spot * 100


def _expiry_dte(expiration: date, as_of_date: date) -> int:
    return max((expiration - as_of_date).days, 0)


def evaluate(snapshot: DeskSnapshot, scenario: ScenarioRequest) -> Evaluation:
    position = snapshot.position
    spot = round(snapshot.spot * (1 + scenario.spot_shift_pct / 100), 2)
    buying_power = round(snapshot.account.options_buying_power * scenario.buying_power_pct / 100, 2)
    width = _spread_width(snapshot)
    max_loss = round((width - position.entry_credit) * 100 * position.quantity, 2)
    close_debit = round(
        max(position.short_leg.quote.ask - position.long_leg.quote.bid, 0), 2
    )
    distance = round(_short_distance_pct(snapshot, spot), 3)
    dte = _expiry_dte(position.short_leg.expiration, scenario.as_of_date or snapshot.as_of.date())
    assignment_notional = round(position.short_leg.strike * 100 * position.quantity, 2)

    roll = snapshot.roll_candidate
    roll_open_credit = round(roll.open_credit, 2) if roll else None
    roll_net_credit = round(roll.open_credit - close_debit, 2) if roll else None
    roll_width = (
        abs(roll.short_leg.strike - roll.long_leg.strike) if roll is not None else 0
    )
    roll_max_loss = (
        round((roll_width - roll.open_credit) * 100 * roll.quantity, 2) if roll else None
    )

    quote_age = max(
        (snapshot.as_of - position.short_leg.quote.timestamp).total_seconds(),
        (snapshot.as_of - position.long_leg.quote.timestamp).total_seconds(),
        0,
    )
    current_quotes_two_sided = all(
        leg.quote.bid > 0
        and leg.quote.ask > 0
        and leg.quote.ask >= leg.quote.bid
        for leg in (position.long_leg, position.short_leg)
    )
    competition_account_ready = abs(snapshot.account.starting_equity - 100_000) < 0.01
    account_ready = (
        snapshot.account.paper
        and snapshot.account.options_trading_level >= 3
        and competition_account_ready
    )
    market_ready = snapshot.market_open
    quote_ready = quote_age <= 120 and current_quotes_two_sided
    width_ready = width > 0 and position.long_leg.expiration == position.short_leg.expiration
    risk_ready = max_loss <= snapshot.account.equity * 0.01
    buying_power_ready = buying_power >= max_loss

    gates = [
        GateResult(
            key="paper-lock",
            label="Paper endpoint lock",
            passed=snapshot.account.paper,
            detail="Account is paper-only" if snapshot.account.paper else "Live account rejected",
        ),
        GateResult(
            key="options-level",
            label="Options level 3",
            passed=snapshot.account.options_trading_level >= 3,
            detail=f"Effective level {snapshot.account.options_trading_level}",
        ),
        GateResult(
            key="competition-account",
            label="$100K competition account",
            passed=competition_account_ready,
            detail=f"Starting equity ${snapshot.account.starting_equity:,.2f}",
        ),
        GateResult(
            key="market-session",
            label="Options session open",
            passed=market_ready,
            detail=f"{scenario.minutes_to_close} minutes until close",
        ),
        GateResult(
            key="quote-freshness",
            label="Fresh two-sided quotes",
            passed=quote_ready,
            detail=f"Oldest quote age {int(quote_age)}s",
        ),
        GateResult(
            key="defined-risk",
            label="Defined-risk vertical",
            passed=width_ready and risk_ready,
            detail=(
                f"Maximum loss ${max_loss:,.0f} "
                f"({max_loss / snapshot.account.equity:.2%} equity)"
            ),
        ),
        GateResult(
            key="buying-power",
            label="Buying-power reserve",
            passed=buying_power_ready,
            detail=f"${buying_power:,.0f} available after scenario",
        ),
    ]

    terminal_spots = [
        ("risk-off", round(position.long_leg.strike - width, 2)),
        ("pin-zone", round(position.short_leg.strike, 2)),
        ("clear", round(position.short_leg.strike + width, 2)),
    ]
    terminal_states = [
        TerminalState(
            label=label,
            spot=value,
            pnl=_payoff(snapshot, value),
            description=(
                "Both legs finish in the money"
                if label == "risk-off"
                else "Short strike is exposed to pin risk"
                if label == "pin-zone"
                else "Spread expires out of the money"
            ),
        )
        for label, value in terminal_spots
    ]

    short_itm = distance < 0
    in_pin_zone = distance < 1.25
    expiry_pressure = dte == 0 and scenario.minutes_to_close <= 120
    hold_allowed = account_ready and quote_ready and not (
        expiry_pressure or short_itm or buying_power < assignment_notional * 0.25
    )
    close_allowed = account_ready and market_ready and quote_ready and width_ready
    roll_distance = _short_distance_pct_for_roll(snapshot, spot)
    roll_quote_age = (
        max(
            (snapshot.as_of - roll.short_leg.quote.timestamp).total_seconds(),
            (snapshot.as_of - roll.long_leg.quote.timestamp).total_seconds(),
            0,
        )
        if roll
        else float("inf")
    )
    roll_quotes_two_sided = bool(
        roll
        and all(
            leg.quote.bid > 0
            and leg.quote.ask > 0
            and leg.quote.ask >= leg.quote.bid
            for leg in (roll.long_leg, roll.short_leg)
        )
    )
    roll_quote_ready = roll_quote_age <= 120 and roll_quotes_two_sided
    roll_allowed = bool(
        roll
        and close_allowed
        and roll_quote_ready
        and buying_power_ready
        and roll_net_credit is not None
        and roll_net_credit >= 0.05
        and roll_max_loss is not None
        and roll_max_loss <= snapshot.account.equity * 0.01
        and roll_distance >= 0.75
    )

    close_pnl = round((position.entry_credit - close_debit) * 100 * position.quantity, 2)
    hold_blockers = []
    if expiry_pressure:
        hold_blockers.append("Inside the 120-minute expiry airlock")
    if short_itm:
        hold_blockers.append("Short option is in the money")
    if buying_power < assignment_notional * 0.25:
        hold_blockers.append("Post-expiry share obligation exceeds reserve policy")
    if not quote_ready:
        hold_blockers.append("Quotes are stale")

    close_blockers = []
    if not market_ready:
        close_blockers.append("Options market is closed")
    if not account_ready:
        close_blockers.append("Paper account or options level check failed")
    if not quote_ready:
        close_blockers.append("Quotes are stale")

    roll_blockers = list(close_blockers)
    if roll is None:
        roll_blockers.append("No later-expiry vertical passed contract selection")
    elif not roll_quote_ready:
        roll_blockers.append("Later-expiry quotes are stale or not two-sided")
    if roll_net_credit is not None and roll_net_credit < 0.05:
        roll_blockers.append("Roll does not collect the minimum $0.05 net credit")
    if roll is not None and roll_distance < 0.75:
        roll_blockers.append("New short strike leaves less than 0.75% spot clearance")
    if not buying_power_ready:
        roll_blockers.append("Insufficient options buying-power reserve")

    hold_risk = min(100, int(35 + max(0, 1.5 - distance) * 24 + (45 if expiry_pressure else 0)))
    close_risk = min(100, max(5, int(20 + close_debit / max(width, 0.01) * 15)))
    roll_risk = min(
        100,
        max(8, int(28 + max(0, 1.25 - roll_distance) * 20 + (0 if roll_allowed else 25))),
    )

    outcomes = [
        ActionOutcome(
            action=Action.HOLD,
            allowed=hold_allowed,
            risk_score=hold_risk,
            immediate_cashflow=0,
            locked_or_max_pnl=round(position.entry_credit * 100 * position.quantity, 2),
            assignment_notional=assignment_notional,
            headline="Keep the current spread into expiry",
            detail=(
                f"At {position.underlying} ${spot:.2f}, the short strike has "
                f"{distance:+.2f}% clearance. "
                "Pin risk remains discontinuous into the close."
            ),
            blockers=hold_blockers,
            terminal_states=terminal_states,
        ),
        ActionOutcome(
            action=Action.CLOSE,
            allowed=close_allowed,
            risk_score=close_risk,
            immediate_cashflow=round(-close_debit * 100 * position.quantity, 2),
            locked_or_max_pnl=close_pnl,
            assignment_notional=0,
            headline="Buy back the vertical and stop the clock",
            detail=(
                f"Conservative two-sided close costs ${close_debit * 100:,.0f}; "
                f"estimated realized P&L becomes ${close_pnl:+,.0f}."
            ),
            blockers=close_blockers,
            terminal_states=[],
        ),
        ActionOutcome(
            action=Action.ROLL,
            allowed=roll_allowed,
            risk_score=roll_risk,
            immediate_cashflow=round((roll_net_credit or 0) * 100 * position.quantity, 2),
            locked_or_max_pnl=round(-(roll_max_loss or max_loss), 2),
            assignment_notional=(
                round(roll.short_leg.strike * 100 * roll.quantity, 2) if roll else 0
            ),
            headline="Atomically close and reopen one week out",
            detail=(
                f"Four legs move as one MLeg order for an estimated ${roll_net_credit * 100:+,.0f} "
                f"net credit and {roll_distance:.2f}% new short-strike clearance."
                if roll_net_credit is not None
                else "No valid later-expiry vertical is currently available."
            ),
            blockers=roll_blockers,
            terminal_states=[],
        ),
    ]

    if expiry_pressure and roll_allowed:
        policy_action = Action.ROLL
    elif (expiry_pressure or short_itm or not buying_power_ready) and close_allowed:
        policy_action = Action.CLOSE
    else:
        policy_action = Action.HOLD if hold_allowed else Action.CLOSE

    urgency = (
        "critical"
        if expiry_pressure and (short_itm or in_pin_zone)
        else "watch"
        if dte <= 1 or in_pin_zone
        else "nominal"
    )

    values = [close_debit, max_loss, spot, buying_power]
    if not all(isfinite(value) for value in values):
        raise ValueError("Evaluation contains a non-finite numeric value")

    return Evaluation(
        evaluated_at=datetime.now(UTC),
        scenario=scenario,
        effective_spot=spot,
        effective_buying_power=buying_power,
        dte=dte,
        short_distance_pct=distance,
        close_debit=close_debit,
        roll_open_credit=roll_open_credit,
        roll_net_credit=roll_net_credit,
        max_loss=max_loss,
        gates=gates,
        outcomes=outcomes,
        policy_action=policy_action,
        urgency=urgency,
    )


def _short_distance_pct_for_roll(snapshot: DeskSnapshot, spot: float) -> float:
    roll = snapshot.roll_candidate
    if roll is None:
        return -100
    if roll.strategy == "bull_put_credit":
        return (spot - roll.short_leg.strike) / spot * 100
    return (roll.short_leg.strike - spot) / spot * 100
