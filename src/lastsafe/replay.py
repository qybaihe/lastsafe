from __future__ import annotations

from datetime import UTC, date, datetime

from .models import (
    AccountSnapshot,
    DeskSnapshot,
    EquityPoint,
    OptionLeg,
    Quote,
    RollCandidate,
    SpreadPosition,
)


def load_replay_snapshot() -> DeskSnapshot:
    """Return a deterministic, clearly labeled expiry incident for public demos."""
    as_of = datetime(2026, 9, 4, 18, 25, tzinfo=UTC)
    current_expiry = date(2026, 9, 4)
    next_expiry = date(2026, 9, 11)

    long_leg = OptionLeg(
        symbol="SPY260904P00635000",
        position_side="long",
        option_type="put",
        strike=635,
        expiration=current_expiry,
        quantity=1,
        entry_price=1.18,
        quote=Quote(bid=1.48, ask=1.58, timestamp=as_of),
        delta=-0.30,
        gamma=0.021,
        theta=-0.24,
        vega=0.09,
    )
    short_leg = OptionLeg(
        symbol="SPY260904P00640000",
        position_side="short",
        option_type="put",
        strike=640,
        expiration=current_expiry,
        quantity=1,
        entry_price=2.60,
        quote=Quote(bid=2.98, ask=3.08, timestamp=as_of),
        delta=-0.54,
        gamma=0.026,
        theta=-0.31,
        vega=0.10,
    )
    position = SpreadPosition(
        id="spy-640-635-260904",
        underlying="SPY",
        strategy="bull_put_credit",
        quantity=1,
        opened_at=datetime(2026, 8, 31, 15, 42, tzinfo=UTC),
        entry_credit=1.42,
        long_leg=long_leg,
        short_leg=short_leg,
    )

    roll_long = OptionLeg(
        symbol="SPY260911P00627000",
        position_side="long",
        option_type="put",
        strike=627,
        expiration=next_expiry,
        quantity=1,
        entry_price=0,
        quote=Quote(bid=1.12, ask=1.25, timestamp=as_of),
        delta=-0.20,
        gamma=0.012,
        theta=-0.15,
        vega=0.16,
    )
    roll_short = OptionLeg(
        symbol="SPY260911P00632000",
        position_side="short",
        option_type="put",
        strike=632,
        expiration=next_expiry,
        quantity=1,
        entry_price=0,
        quote=Quote(bid=3.10, ask=3.22, timestamp=as_of),
        delta=-0.28,
        gamma=0.015,
        theta=-0.21,
        vega=0.18,
    )
    roll_candidate = RollCandidate(
        underlying="SPY",
        strategy="bull_put_credit",
        quantity=1,
        open_credit=1.85,
        long_leg=roll_long,
        short_leg=roll_short,
    )

    curve_values = [
        (datetime(2026, 8, 28, 20, 0, tzinfo=UTC), 100000.00),
        (datetime(2026, 8, 31, 20, 0, tzinfo=UTC), 100118.40),
        (datetime(2026, 9, 1, 20, 0, tzinfo=UTC), 100076.10),
        (datetime(2026, 9, 2, 20, 0, tzinfo=UTC), 100294.70),
        (datetime(2026, 9, 3, 20, 0, tzinfo=UTC), 100512.60),
        (as_of, 100482.30),
    ]

    return DeskSnapshot(
        source="replay",
        source_label="Seeded expiry incident · indicative-style quotes · not live P&L",
        as_of=as_of,
        market_open=True,
        minutes_to_close=95,
        spot=639.40,
        account=AccountSnapshot(
            account_id="REPLAY-7F2A",
            equity=100482.30,
            starting_equity=100000,
            cash=100340.30,
            buying_power=61800,
            options_buying_power=28500,
            options_trading_level=3,
            daily_pnl=-30.30,
            total_pnl=482.30,
            max_drawdown_pct=-0.42,
            paper=True,
            starting_equity_verified=True,
        ),
        position=position,
        roll_candidate=roll_candidate,
        equity_curve=[EquityPoint(timestamp=point[0], equity=point[1]) for point in curve_values],
    )
