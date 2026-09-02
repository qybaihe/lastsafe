from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .engine import ParsedOptionSymbol, parse_occ_symbol
from .models import (
    AccountSnapshot,
    DeskSnapshot,
    EntryCandidate,
    EquityPoint,
    OptionLeg,
    Quote,
    RollCandidate,
    SpreadPosition,
)


class AlpacaError(RuntimeError):
    pass


class AlpacaClient:
    trading_base = "https://paper-api.alpaca.markets"
    data_base = "https://data.alpaca.markets"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
        }

    async def _get(self, url: str, *, params: dict | None = None) -> dict | list:
        async with httpx.AsyncClient(timeout=20, headers=self.headers) as client:
            response = await client.get(url, params=params)
            if response.status_code >= 400:
                message = self._error_message(response)
                raise AlpacaError(f"Alpaca returned {response.status_code}: {message}")
            return response.json()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.reason_phrase or "request failed"
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("code") or "request failed")
        return "request failed"

    async def snapshot(self) -> DeskSnapshot:
        account_raw, positions_raw, clock_raw, history_raw, orders_raw = await asyncio.gather(
            self._get(f"{self.trading_base}/v2/account"),
            self._get(f"{self.trading_base}/v2/positions"),
            self._get(f"{self.trading_base}/v2/clock"),
            self._get(
                f"{self.trading_base}/v2/account/portfolio/history",
                params={"period": "1W", "timeframe": "1H", "intraday_reporting": "market_hours"},
            ),
            self._get(
                f"{self.trading_base}/v2/orders",
                params={"status": "open", "nested": "true", "limit": 100},
            ),
        )
        assert isinstance(account_raw, dict)
        assert isinstance(positions_raw, list)
        assert isinstance(clock_raw, dict)
        assert isinstance(history_raw, dict)
        assert isinstance(orders_raw, list)

        if self.settings.expected_account_id and str(account_raw.get("id")) != (
            self.settings.expected_account_id
        ):
            raise AlpacaError("Connected paper account does not match LASTSAFE_EXPECTED_ACCOUNT_ID")

        spread, portfolio_issues = self._select_vertical(positions_raw)
        campaign_id = None
        if spread is not None:
            campaign_id = await self._campaign_id(spread, account_raw)
            spread.id = campaign_id

        underlying = spread.underlying if spread else "SPY"
        stock_snapshot = await self._get(
            f"{self.data_base}/v2/stocks/{underlying}/snapshot",
            params={"feed": "iex"},
        )
        assert isinstance(stock_snapshot, dict)
        latest_trade = stock_snapshot.get("latestTrade") or {}
        spot = float(latest_trade.get("p") or stock_snapshot.get("dailyBar", {}).get("c") or 0)
        if spot <= 0:
            raise AlpacaError("Underlying spot price is unavailable")
        roll = None
        entry_candidate = None
        if spread is not None:
            symbols = f"{spread.long_leg.symbol},{spread.short_leg.symbol}"
            quotes_raw = await self._get(
                f"{self.data_base}/v1beta1/options/quotes/latest",
                params={"symbols": symbols, "feed": self.settings.alpaca_data_feed},
            )
            assert isinstance(quotes_raw, dict)
            quote_map = quotes_raw.get("quotes", {})
            spread.long_leg.quote = self._quote(quote_map.get(spread.long_leg.symbol))
            spread.short_leg.quote = self._quote(quote_map.get(spread.short_leg.symbol))
            roll = await self._find_roll(spread, spot)
        elif not portfolio_issues and not orders_raw:
            entry_candidate = await self._find_entry_candidate(spot)

        next_close = datetime.fromisoformat(str(clock_raw["next_close"]).replace("Z", "+00:00"))
        now = datetime.now(UTC)
        minutes_to_close = max(
            0, int((next_close.astimezone(UTC) - now).total_seconds() / 60)
        )
        equity = float(account_raw["equity"])
        last_equity = float(account_raw.get("last_equity") or equity)
        starting_equity = self._starting_equity(history_raw, equity)
        daily_pnl = equity - last_equity

        curve = [
            EquityPoint(timestamp=datetime.fromtimestamp(ts, tz=UTC), equity=float(value))
            for ts, value in zip(
                history_raw.get("timestamp", []), history_raw.get("equity", []), strict=False
            )
            if value is not None
        ]
        if not curve:
            curve = [EquityPoint(timestamp=now, equity=equity)]

        return DeskSnapshot(
            source="alpaca",
            source_label=f"Alpaca paper · {self.settings.alpaca_data_feed} options feed",
            as_of=now,
            market_open=bool(clock_raw["is_open"]),
            minutes_to_close=minutes_to_close,
            spot=spot,
            account=AccountSnapshot(
                account_id=str(account_raw["id"]),
                equity=equity,
                starting_equity=starting_equity,
                cash=float(account_raw["cash"]),
                buying_power=float(account_raw["buying_power"]),
                options_buying_power=self._required_nonnegative(
                    account_raw.get("options_buying_power"), "options buying power"
                ),
                options_trading_level=int(account_raw.get("options_trading_level") or 0),
                daily_pnl=daily_pnl,
                total_pnl=equity - starting_equity,
                max_drawdown_pct=self._max_drawdown(curve),
                paper=True,
                status=str(account_raw.get("status") or "UNKNOWN"),
                trading_blocked=any(
                    bool(account_raw.get(field))
                    for field in (
                        "account_blocked",
                        "trading_blocked",
                        "trade_suspended_by_user",
                    )
                ),
                starting_equity_verified=False,
            ),
            position=spread,
            roll_candidate=roll,
            entry_candidate=entry_candidate,
            open_order_count=len(orders_raw),
            portfolio_issues=portfolio_issues,
            equity_curve=curve,
        )

    async def _campaign_id(self, spread: SpreadPosition, account: dict) -> str:
        try:
            activities = await self._get(
                f"{self.trading_base}/v2/account/activities/FILL",
                params={"direction": "desc", "page_size": 100},
            )
        except AlpacaError:
            activities = []
        rows = activities if isinstance(activities, list) else []
        symbols = {spread.long_leg.symbol, spread.short_leg.symbol}
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("symbol")) in symbols
        ]
        fill_ids = sorted(
            str(row.get("id") or row.get("order_id") or row.get("transaction_time") or "")
            for row in matches
        )
        if not any(fill_ids):
            # Legacy/manual positions remain manageable but are scoped to the first observed basis.
            fill_ids = [
                f"{spread.long_leg.symbol}:{spread.long_leg.entry_price}",
                f"{spread.short_leg.symbol}:{spread.short_leg.entry_price}",
            ]
        seed = f"{account.get('id')}|{'|'.join(fill_ids)}"
        return f"campaign-{hashlib.sha256(seed.encode()).hexdigest()[:20]}"

    async def competition_preflight(self) -> dict:
        account, positions, orders = await asyncio.gather(
            self._get(f"{self.trading_base}/v2/account"),
            self._get(f"{self.trading_base}/v2/positions"),
            self._get(
                f"{self.trading_base}/v2/orders",
                params={"status": "open", "nested": "true", "limit": 100},
            ),
        )
        if not isinstance(account, dict) or not isinstance(positions, list) or not isinstance(
            orders, list
        ):
            raise AlpacaError("Competition preflight returned an unexpected response")
        account_id = str(account.get("id") or "")
        if not account_id:
            raise AlpacaError("Competition account ID is missing")
        if self.settings.expected_account_id and account_id != self.settings.expected_account_id:
            raise AlpacaError("Competition account does not match LASTSAFE_EXPECTED_ACCOUNT_ID")
        equity = float(account.get("equity") or 0)
        cash = float(account.get("cash") or 0)
        options_level = int(account.get("options_trading_level") or 0)
        blockers = []
        if str(account.get("status") or "").upper() not in {"ACTIVE", "PAPER_ONLY"}:
            blockers.append("account is not active")
        if abs(equity - 100_000) >= 0.01 or abs(cash - 100_000) >= 0.01:
            blockers.append("equity and cash must both be exactly $100,000")
        if positions:
            blockers.append("competition account must have zero positions at enrollment")
        if orders:
            blockers.append("competition account must have zero open orders at enrollment")
        if options_level < 3:
            blockers.append("options trading level 3 is required")
        if any(
            bool(account.get(field))
            for field in ("account_blocked", "trading_blocked", "trade_suspended_by_user")
        ):
            blockers.append("account trading is blocked")
        if blockers:
            raise AlpacaError("Competition account enrollment refused: " + "; ".join(blockers))
        captured_at = datetime.now(UTC).isoformat()
        canonical = json.dumps(
            {
                "account_id": account_id,
                "captured_at": captured_at,
                "cash": cash,
                "equity": equity,
                "options_trading_level": options_level,
                "positions": 0,
                "open_orders": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "account_id": account_id,
            "captured_at": captured_at,
            "cash": cash,
            "equity": equity,
            "options_trading_level": options_level,
            "positions": 0,
            "open_orders": 0,
            "preflight_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        }

    def _select_vertical(
        self, positions: list[dict]
    ) -> tuple[SpreadPosition | None, list[str]]:
        option_positions: list[tuple[dict, ParsedOptionSymbol]] = []
        issues: list[str] = []
        for position in positions:
            symbol = str(position.get("symbol", ""))
            try:
                parsed = parse_occ_symbol(symbol)
            except ValueError:
                issues.append(f"Unsupported position {symbol or '<missing symbol>'}")
                continue
            option_positions.append((position, parsed))
        if issues:
            return None, issues
        if not option_positions:
            return None, []
        if len(option_positions) != 2:
            return None, [
                f"Expected exactly two option legs; broker reports {len(option_positions)}"
            ]
        long_items = [item for item in option_positions if float(item[0]["qty"]) > 0]
        short_items = [item for item in option_positions if float(item[0]["qty"]) < 0]
        if len(long_items) != 1 or len(short_items) != 1:
            return None, ["Broker positions are not one long and one short option leg"]
        long_position, long_parsed = long_items[0]
        short_position, short_parsed = short_items[0]
        long_qty = abs(int(float(long_position["qty"])))
        short_qty = abs(int(float(short_position["qty"])))
        if long_qty != short_qty:
            return None, [f"Unmatched option quantities: long {long_qty}, short {short_qty}"]
        if (
            long_parsed.underlying != short_parsed.underlying
            or long_parsed.expiration != short_parsed.expiration
            or long_parsed.option_type != short_parsed.option_type
        ):
            return None, ["Option legs do not form one same-underlying, same-expiry vertical"]
        strategy = (
            "bull_put_credit"
            if short_parsed.option_type == "put" and short_parsed.strike > long_parsed.strike
            else "bear_call_credit"
            if short_parsed.option_type == "call" and short_parsed.strike < long_parsed.strike
            else None
        )
        if strategy is None:
            return None, ["Long leg does not protect the short option"]
        long_price = float(long_position.get("avg_entry_price") or 0)
        short_price = float(short_position.get("avg_entry_price") or 0)
        entry_credit = short_price - long_price
        width = abs(short_parsed.strike - long_parsed.strike)
        if not 0 < entry_credit < width:
            return None, ["Broker entry prices imply invalid vertical-spread economics"]
        placeholder = Quote(bid=0, ask=0, timestamp=datetime.now(UTC))
        try:
            spread = SpreadPosition(
                id=(
                    f"{short_parsed.underlying}-{short_parsed.expiration}-"
                    f"{short_parsed.strike}-{long_parsed.strike}"
                ),
                underlying=short_parsed.underlying,
                strategy=strategy,
                quantity=long_qty,
                opened_at=self._opened_at(long_position, short_position),
                entry_credit=entry_credit,
                long_leg=OptionLeg(
                    symbol=str(long_position["symbol"]),
                    position_side="long",
                    option_type=long_parsed.option_type,
                    strike=long_parsed.strike,
                    expiration=long_parsed.expiration,
                    quantity=long_qty,
                    entry_price=long_price,
                    quote=placeholder,
                ),
                short_leg=OptionLeg(
                    symbol=str(short_position["symbol"]),
                    position_side="short",
                    option_type=short_parsed.option_type,
                    strike=short_parsed.strike,
                    expiration=short_parsed.expiration,
                    quantity=short_qty,
                    entry_price=short_price,
                    quote=placeholder,
                ),
            )
        except ValueError as error:
            return None, [str(error)]
        return spread, []

    @staticmethod
    def _opened_at(*positions: dict) -> datetime:
        timestamps = []
        for position in positions:
            for key in ("created_at", "opened_at", "updated_at"):
                value = position.get(key)
                if value:
                    with suppress(ValueError):
                        timestamps.append(AlpacaClient._parse_timestamp(str(value)))
                    break
        return min(timestamps) if timestamps else datetime.now(UTC)

    async def _find_roll(self, spread: SpreadPosition, spot: float) -> RollCandidate | None:
        min_expiry = spread.short_leg.expiration + timedelta(days=5)
        max_expiry = spread.short_leg.expiration + timedelta(days=12)
        current_width = abs(spread.short_leg.strike - spread.long_leg.strike)
        strike_buffer = max(current_width * 4, spot * 0.06)
        common_filters = {
            "type": spread.short_leg.option_type,
            "expiration_date_gte": str(min_expiry),
            "expiration_date_lte": str(max_expiry),
            "strike_price_gte": round(spot - strike_buffer, 2),
            "strike_price_lte": round(spot + strike_buffer, 2),
        }
        chain, contract_response = await asyncio.gather(
            self._get(
                f"{self.data_base}/v1beta1/options/snapshots/{spread.underlying}",
                params={
                    **common_filters,
                    "feed": self.settings.alpaca_data_feed,
                    "limit": 1000,
                },
            ),
            self._get(
                f"{self.trading_base}/v2/options/contracts",
                params={
                    "underlying_symbols": spread.underlying,
                    **common_filters,
                    "status": "active",
                    "show_deliverables": "true",
                    "limit": 10_000,
                },
            ),
        )
        assert isinstance(chain, dict)
        assert isinstance(contract_response, dict)
        eligible_symbols = {
            str(contract["symbol"])
            for contract in contract_response.get("option_contracts", [])
            if isinstance(contract, dict)
            and contract.get("tradable") is True
            and contract.get("status") == "active"
            and float(contract.get("multiplier") or 0) == 100
            and float(contract.get("size") or 0) == 100
            and self._has_standard_deliverable(contract)
        }
        snapshots = chain.get("snapshots", {})
        if not isinstance(snapshots, dict):
            return None

        contracts: list[tuple[ParsedOptionSymbol, str, dict]] = []
        for symbol, snapshot in snapshots.items():
            if symbol not in eligible_symbols:
                continue
            if not isinstance(snapshot, dict):
                continue
            quote = snapshot.get("latestQuote")
            if not isinstance(quote, dict):
                continue
            try:
                parsed = parse_occ_symbol(symbol)
                bid = float(quote.get("bp") or 0)
                ask = float(quote.get("ap") or 0)
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            contracts.append((parsed, symbol, snapshot))

        candidates: list[
            tuple[
                float,
                float,
                ParsedOptionSymbol,
                str,
                dict,
                ParsedOptionSymbol,
                str,
                dict,
            ]
        ] = []
        for short_parsed, short_symbol, short_snapshot in contracts:
            clearance = (
                (spot - short_parsed.strike) / spot * 100
                if spread.strategy == "bull_put_credit"
                else (short_parsed.strike - spot) / spot * 100
            )
            if clearance < 0.75:
                continue
            for long_parsed, long_symbol, long_snapshot in contracts:
                if long_parsed.expiration != short_parsed.expiration:
                    continue
                width = abs(short_parsed.strike - long_parsed.strike)
                if abs(width - current_width) > 0.01:
                    continue
                protected = (
                    long_parsed.strike < short_parsed.strike
                    if spread.strategy == "bull_put_credit"
                    else long_parsed.strike > short_parsed.strike
                )
                if not protected:
                    continue
                short_bid = float(short_snapshot["latestQuote"]["bp"])
                long_ask = float(long_snapshot["latestQuote"]["ap"])
                open_credit = round(short_bid - long_ask, 2)
                if open_credit < 0.10 or open_credit >= width:
                    continue
                spread_pct = (
                    (float(short_snapshot["latestQuote"]["ap"]) - short_bid)
                    + (
                        float(long_snapshot["latestQuote"]["ap"])
                        - float(long_snapshot["latestQuote"]["bp"])
                    )
                ) / open_credit
                if spread_pct > 0.85:
                    continue
                score = open_credit + min(clearance, 3) * 0.05 - spread_pct * 0.15
                candidates.append(
                    (
                        score,
                        open_credit,
                        short_parsed,
                        short_symbol,
                        short_snapshot,
                        long_parsed,
                        long_symbol,
                        long_snapshot,
                    )
                )
        if not candidates:
            return None
        (
            _,
            open_credit,
            short_parsed,
            short_symbol,
            short_snapshot,
            long_parsed,
            long_symbol,
            long_snapshot,
        ) = max(candidates, key=lambda candidate: candidate[0])
        return RollCandidate(
            underlying=spread.underlying,
            strategy=spread.strategy,
            quantity=spread.quantity,
            open_credit=open_credit,
            long_leg=self._chain_leg(
                long_symbol, long_parsed, long_snapshot, "long", spread.quantity
            ),
            short_leg=self._chain_leg(
                short_symbol, short_parsed, short_snapshot, "short", spread.quantity
            ),
        )

    async def _find_entry_candidate(self, spot: float) -> EntryCandidate | None:
        today = datetime.now(ZoneInfo("America/New_York")).date()
        min_expiry = today + timedelta(days=2)
        max_expiry = today + timedelta(days=7)
        bars_raw, chain, contract_response = await asyncio.gather(
            self._get(
                f"{self.data_base}/v2/stocks/SPY/bars",
                params={
                    "timeframe": "1Day",
                    "start": str(today - timedelta(days=45)),
                    "end": str(today + timedelta(days=1)),
                    "limit": 40,
                    "feed": "iex",
                },
            ),
            self._get(
                f"{self.data_base}/v1beta1/options/snapshots/SPY",
                params={
                    "expiration_date_gte": str(min_expiry),
                    "expiration_date_lte": str(max_expiry),
                    "strike_price_gte": round(spot * 0.94, 2),
                    "strike_price_lte": round(spot * 1.06, 2),
                    "feed": self.settings.alpaca_data_feed,
                    "limit": 1000,
                },
            ),
            self._get(
                f"{self.trading_base}/v2/options/contracts",
                params={
                    "underlying_symbols": "SPY",
                    "expiration_date_gte": str(min_expiry),
                    "expiration_date_lte": str(max_expiry),
                    "strike_price_gte": round(spot * 0.94, 2),
                    "strike_price_lte": round(spot * 1.06, 2),
                    "status": "active",
                    "show_deliverables": "true",
                    "limit": 10_000,
                },
            ),
        )
        assert isinstance(bars_raw, dict)
        assert isinstance(chain, dict)
        assert isinstance(contract_response, dict)
        bars = bars_raw.get("bars", [])
        closes = [float(bar["c"]) for bar in bars if isinstance(bar, dict) and bar.get("c")]
        if len(closes) < 20:
            return None
        trend_5d = (closes[-1] / closes[-6] - 1) * 100
        sma20 = sum(closes[-20:]) / 20
        price_vs_sma20 = (spot / sma20 - 1) * 100
        if abs(trend_5d) < 0.15:
            return None
        strategy = (
            "bull_put_credit"
            if trend_5d > 0 and price_vs_sma20 > 0
            else "bear_call_credit"
            if trend_5d < 0 and price_vs_sma20 < 0
            else None
        )
        if strategy is None:
            return None

        eligible = {
            str(contract["symbol"]): contract
            for contract in contract_response.get("option_contracts", [])
            if isinstance(contract, dict)
            and contract.get("tradable") is True
            and contract.get("status") == "active"
            and float(contract.get("multiplier") or 0) == 100
            and float(contract.get("size") or 0) == 100
            and self._has_standard_deliverable(contract)
        }
        snapshots = chain.get("snapshots", {})
        if not isinstance(snapshots, dict):
            return None
        option_type = "put" if strategy == "bull_put_credit" else "call"
        candidates: list[tuple[float, EntryCandidate]] = []
        parsed_rows: list[tuple[ParsedOptionSymbol, str, dict, dict]] = []
        for symbol, snapshot in snapshots.items():
            if symbol not in eligible or not isinstance(snapshot, dict):
                continue
            try:
                parsed = parse_occ_symbol(symbol)
            except ValueError:
                continue
            if parsed.option_type != option_type:
                continue
            quote = snapshot.get("latestQuote")
            if not isinstance(quote, dict):
                continue
            bid = float(quote.get("bp") or 0)
            ask = float(quote.get("ap") or 0)
            delta = abs(float((snapshot.get("greeks") or {}).get("delta") or 0))
            if bid <= 0 or ask <= bid or not 0.10 <= delta <= 0.35:
                continue
            mid = (bid + ask) / 2
            if mid <= 0 or (ask - bid) / mid > 0.25:
                continue
            parsed_rows.append((parsed, symbol, snapshot, eligible[symbol]))

        for short_parsed, short_symbol, short_snapshot, _ in parsed_rows:
            short_delta = abs(float((short_snapshot.get("greeks") or {}).get("delta") or 0))
            if not 0.15 <= short_delta <= 0.28:
                continue
            clearance = (
                (spot - short_parsed.strike) / spot * 100
                if strategy == "bull_put_credit"
                else (short_parsed.strike - spot) / spot * 100
            )
            if clearance < 1:
                continue
            for long_parsed, long_symbol, long_snapshot, _ in parsed_rows:
                if long_parsed.expiration != short_parsed.expiration:
                    continue
                protected = (
                    long_parsed.strike < short_parsed.strike
                    if strategy == "bull_put_credit"
                    else long_parsed.strike > short_parsed.strike
                )
                width = abs(short_parsed.strike - long_parsed.strike)
                if not protected or not 2 <= width <= 10:
                    continue
                short_bid = float(short_snapshot["latestQuote"]["bp"])
                long_ask = float(long_snapshot["latestQuote"]["ap"])
                credit = round(short_bid - long_ask, 2)
                if credit <= 0 or credit / width < 0.15:
                    continue
                max_loss = round((width - credit) * 100, 2)
                if max_loss > 500:
                    continue
                dte = (short_parsed.expiration - today).days
                rationale = (
                    f"SPY {trend_5d:+.2f}% over five sessions and {price_vs_sma20:+.2f}% "
                    f"versus SMA20; one {strategy.replace('_', ' ')} with "
                    f"{clearance:.2f}% short-strike clearance and ${max_loss:.0f} max loss."
                )
                try:
                    candidate = EntryCandidate(
                        id=(
                            f"entry-{short_parsed.expiration}-{short_parsed.strike}-"
                            f"{long_parsed.strike}"
                        ),
                        underlying="SPY",
                        strategy=strategy,
                        quantity=1,
                        open_credit=credit,
                        max_loss=max_loss,
                        dte=dte,
                        short_clearance_pct=clearance,
                        trend_5d_pct=trend_5d,
                        price_vs_sma20_pct=price_vs_sma20,
                        rationale=rationale,
                        long_leg=self._chain_leg(
                            long_symbol, long_parsed, long_snapshot, "long", 1
                        ),
                        short_leg=self._chain_leg(
                            short_symbol, short_parsed, short_snapshot, "short", 1
                        ),
                    )
                except ValueError:
                    continue
                score = credit * 100 / max_loss + min(clearance, 4) * 0.002
                candidates.append((score, candidate))
        return max(candidates, key=lambda row: row[0])[1] if candidates else None

    @staticmethod
    def _has_standard_deliverable(contract: dict) -> bool:
        deliverables = contract.get("deliverables")
        if deliverables is None:
            return True
        return bool(
            len(deliverables) == 1
            and deliverables[0].get("type") == "equity"
            and deliverables[0].get("symbol") == contract.get("underlying_symbol")
            and float(deliverables[0].get("amount") or 0) == 100
            and deliverables[0].get("delayed_settlement") is False
        )

    def _chain_leg(
        self,
        symbol: str,
        parsed: ParsedOptionSymbol,
        snapshot: dict,
        side: str,
        quantity: int,
    ) -> OptionLeg:
        greeks = snapshot.get("greeks") or {}
        return OptionLeg(
            symbol=symbol,
            position_side=side,
            option_type=parsed.option_type,
            strike=parsed.strike,
            expiration=parsed.expiration,
            quantity=quantity,
            entry_price=0,
            quote=self._quote(snapshot.get("latestQuote")),
            delta=greeks.get("delta"),
            gamma=greeks.get("gamma"),
            theta=greeks.get("theta"),
            vega=greeks.get("vega"),
        )

    def _quote(self, raw: dict | None) -> Quote:
        if not raw:
            raise AlpacaError("Required option quote is unavailable")
        timestamp = self._parse_timestamp(str(raw["t"]))
        return Quote(
            bid=float(raw["bp"]),
            ask=float(raw["ap"]),
            timestamp=timestamp,
            feed=self.settings.alpaca_data_feed,
        )

    @staticmethod
    def _starting_equity(history: dict, fallback: float) -> float:
        values = [
            float(value)
            for value in history.get("equity", [])
            if value is not None and float(value) > 0
        ]
        base_value = float(history.get("base_value") or 0)
        if base_value > 0:
            return base_value
        return values[0] if values else fallback

    @staticmethod
    def _max_drawdown(curve: list[EquityPoint]) -> float:
        positive = [point for point in curve if point.equity > 0]
        if not positive:
            return 0
        peak = positive[0].equity
        drawdown = 0.0
        for point in positive:
            peak = max(peak, point.equity)
            drawdown = min(drawdown, (point.equity - peak) / peak * 100)
        return round(drawdown, 3)

    @staticmethod
    def _required_nonnegative(value, label: str) -> float:
        if value is None:
            raise AlpacaError(f"{label} is unavailable")
        parsed = float(value)
        if parsed < 0:
            return 0
        return parsed

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        normalized = value.rstrip("Z")
        if "." in normalized:
            prefix, fractional = normalized.split(".", 1)
            normalized = f"{prefix}.{fractional[:6]}"
        return datetime.fromisoformat(f"{normalized}+00:00")

    async def execute_via_cli(
        self,
        action: str,
        snapshot: DeskSnapshot,
        client_order_id: str,
        *,
        attempt: int = 1,
        mutation_guard: Callable[[], bool] | None = None,
        prepare_submission: Callable[[], Awaitable[DeskSnapshot]] | None = None,
    ) -> tuple[str, dict, str, bool, list[dict]]:
        if action in {"HOLD", "STAND_DOWN"}:
            return "", {}, f"{action} never creates an order", False, []
        args = self._cli_args(action, snapshot, client_order_id)
        lifecycle: list[dict] = []
        existing = await self._query_cli_order(client_order_id)
        if existing is not None:
            lifecycle.append(self._lifecycle_event(existing, "recovered"))
            if self._status(existing) not in self._retryable_terminal_statuses():
                terminal = await self._wait_for_terminal(existing, lifecycle)
                return (
                    self.command_preview(action, snapshot, client_order_id),
                    terminal,
                    "Existing Alpaca paper order recovered by client order ID",
                    True,
                    lifecycle,
                )
            return (
                self.command_preview(action, snapshot, client_order_id),
                existing,
                f"Existing Alpaca order is {self._status(existing)}; a numbered retry is required",
                True,
                lifecycle,
            )
        if prepare_submission is not None:
            snapshot = await prepare_submission()
            args = self._cli_args(action, snapshot, client_order_id)
        if mutation_guard is not None and not mutation_guard():
            raise AlpacaError("Execution lease was lost before CLI order submission")
        payload = await self._run_cli(args, allow_not_found=False)
        if payload is None:
            raise AlpacaError("Alpaca CLI did not return an order")
        lifecycle.append(self._lifecycle_event(payload, "submitted"))
        payload = await self._wait_for_terminal(payload, lifecycle)
        status = self._status(payload)
        detail = (
            "Alpaca CLI order filled and reconciled"
            if status == "filled"
            else f"Alpaca CLI order reached terminal status {status}"
            if status in self._terminal_statuses()
            else f"Alpaca CLI order remains {status} after monitoring timeout"
        )
        return (
            self.command_preview(action, snapshot, client_order_id),
            payload,
            detail,
            False,
            lifecycle,
        )

    async def _wait_for_terminal(self, order: dict, lifecycle: list[dict]) -> dict:
        client_order_id = str(order.get("client_order_id") or "")
        if not client_order_id:
            return order
        deadline = time.monotonic() + self.settings.order_timeout_seconds
        current = order
        seen = self._status(current)
        while seen not in self._terminal_statuses() and time.monotonic() < deadline:
            await asyncio.sleep(self.settings.order_poll_seconds)
            refreshed = await self._query_cli_order(client_order_id)
            if refreshed is None:
                continue
            status = self._status(refreshed)
            if status != seen or refreshed.get("filled_qty") != current.get("filled_qty"):
                lifecycle.append(self._lifecycle_event(refreshed, "broker_update"))
            current, seen = refreshed, status
        if seen not in self._terminal_statuses():
            order_id = str(current.get("id") or "")
            if order_id:
                await self._cancel_cli_order(order_id)
                lifecycle.append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "event": "cancel_requested",
                        "status": seen,
                        "order_id": order_id,
                    }
                )
                for _ in range(5):
                    await asyncio.sleep(self.settings.order_poll_seconds)
                    refreshed = await self._query_cli_order(client_order_id)
                    if refreshed is None:
                        continue
                    current = refreshed
                    lifecycle.append(self._lifecycle_event(refreshed, "cancel_reconcile"))
                    if self._status(refreshed) in self._terminal_statuses():
                        break
        return current

    async def _cancel_cli_order(self, order_id: str) -> None:
        await self._run_cli(
            [self.settings.alpaca_cli_path, "order", "cancel", "--order-id", order_id],
            allow_not_found=True,
            allow_empty=True,
        )

    async def positions_via_cli(self) -> list[dict]:
        payload = await self._run_cli(
            [self.settings.alpaca_cli_path, "position", "list"],
            allow_not_found=False,
        )
        if not isinstance(payload, list):
            raise AlpacaError("Alpaca CLI position list returned an unexpected response")
        return [row for row in payload if isinstance(row, dict)]

    async def _query_cli_order(self, client_order_id: str) -> dict | None:
        payload = await self._run_cli(
            [
                self.settings.alpaca_cli_path,
                "order",
                "get-by-client-id",
                "--client-order-id",
                client_order_id,
            ],
            allow_not_found=True,
        )
        if payload is not None and not isinstance(payload, dict):
            raise AlpacaError("Alpaca CLI order lookup returned an unexpected response")
        return payload

    async def _run_cli(
        self,
        args: list[str],
        *,
        allow_not_found: bool,
        allow_empty: bool = False,
    ) -> dict | list | None:
        env = {
            **os.environ,
            "ALPACA_API_KEY": self.settings.alpaca_api_key,
            "ALPACA_SECRET_KEY": self.settings.alpaca_secret_key,
            "ALPACA_LIVE_TRADE": "false",
            "ALPACA_OUTPUT": "json",
        }
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            message = self._sanitize_cli_error(stderr.decode().strip())
            if allow_not_found and self._is_not_found(message):
                return None
            raise AlpacaError(message or "Alpaca CLI execution failed")
        if allow_empty and not stdout.strip():
            return {}
        try:
            payload = json.loads(stdout.decode())
        except json.JSONDecodeError as error:
            raise AlpacaError("Alpaca CLI returned invalid JSON") from error
        if not isinstance(payload, (dict, list)):
            raise AlpacaError("Alpaca CLI returned an unexpected response")
        return payload

    @staticmethod
    def _status(order: dict) -> str:
        return str(order.get("status") or "unknown").strip().lower()

    @staticmethod
    def _terminal_statuses() -> set[str]:
        return {
            "filled",
            "canceled",
            "cancelled",
            "expired",
            "rejected",
            "suspended",
            "done_for_day",
            "replaced",
            "calculated",
        }

    @staticmethod
    def _retryable_terminal_statuses() -> set[str]:
        return {
            "canceled",
            "cancelled",
            "expired",
            "rejected",
            "suspended",
            "done_for_day",
        }

    @classmethod
    def _lifecycle_event(cls, order: dict, event: str) -> dict:
        return {
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "status": cls._status(order),
            "order_id": order.get("id"),
            "client_order_id": order.get("client_order_id"),
            "filled_qty": order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"),
        }

    @staticmethod
    def _is_not_found(message: str) -> bool:
        normalized = message.lower()
        return "404" in normalized or "not found" in normalized

    @staticmethod
    def _sanitize_cli_error(message: str) -> str:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return message[:400]
        if not isinstance(payload, dict):
            return "Alpaca CLI request failed"
        return str(payload.get("message") or payload.get("error") or payload.get("code"))[:400]

    @staticmethod
    def client_order_id(action: str, snapshot: DeskSnapshot, *, attempt: int = 1) -> str:
        digest = hashlib.sha256(
            AlpacaClient.incident_key(action, snapshot).encode("utf-8")
        ).hexdigest()[:20]
        return f"lastsafe-{action.lower()}-{digest}-a{attempt}"

    @staticmethod
    def incident_key(action: str, snapshot: DeskSnapshot) -> str:
        roll_symbols = ""
        if action == "ROLL" and snapshot.roll_candidate is not None:
            roll_symbols = (
                f"-{snapshot.roll_candidate.long_leg.symbol}"
                f"-{snapshot.roll_candidate.short_leg.symbol}"
            )
        position_id = snapshot.position.id if snapshot.position else (
            snapshot.entry_candidate.id if snapshot.entry_candidate else "flat"
        )
        return f"{snapshot.account.account_id}-{position_id}-{action}{roll_symbols}"

    def command_preview(self, action: str, snapshot: DeskSnapshot, client_order_id: str) -> str:
        args = self._cli_args(action, snapshot, client_order_id)
        return shlex.join(args)

    def _cli_args(self, action: str, snapshot: DeskSnapshot, client_order_id: str) -> list[str]:
        if action == "OPEN":
            candidate = snapshot.entry_candidate
            if candidate is None:
                raise AlpacaError("No deterministic entry candidate is available")
            return self._order_args(
                quantity=candidate.quantity,
                limit_price=-candidate.open_credit,
                client_order_id=client_order_id,
                legs=[
                    {
                        "symbol": candidate.long_leg.symbol,
                        "ratio_qty": "1",
                        "side": "buy",
                        "position_intent": "buy_to_open",
                    },
                    {
                        "symbol": candidate.short_leg.symbol,
                        "ratio_qty": "1",
                        "side": "sell",
                        "position_intent": "sell_to_open",
                    },
                ],
            )
        position = snapshot.position
        if position is None:
            raise AlpacaError(f"{action} requires an open spread")
        close_limit = round(
            max(position.short_leg.quote.ask - position.long_leg.quote.bid, 0), 2
        )
        close_legs = [
            {
                "symbol": position.short_leg.symbol,
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_close",
            },
            {
                "symbol": position.long_leg.symbol,
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_close",
            },
        ]
        limit_price = close_limit
        legs = close_legs
        if action == "ROLL":
            roll = snapshot.roll_candidate
            if roll is None:
                raise AlpacaError("No deterministic roll candidate is available")
            net_credit = round(roll.open_credit - close_limit, 2)
            limit_price = -net_credit
            legs += [
                {
                    "symbol": roll.long_leg.symbol,
                    "ratio_qty": "1",
                    "side": "buy",
                    "position_intent": "buy_to_open",
                },
                {
                    "symbol": roll.short_leg.symbol,
                    "ratio_qty": "1",
                    "side": "sell",
                    "position_intent": "sell_to_open",
                },
            ]
        return self._order_args(
            quantity=position.quantity,
            limit_price=limit_price,
            client_order_id=client_order_id,
            legs=legs,
        )

    def _order_args(
        self,
        *,
        quantity: int,
        limit_price: float,
        client_order_id: str,
        legs: list[dict],
    ) -> list[str]:
        return [
            self.settings.alpaca_cli_path,
            "order",
            "submit",
            "--type",
            "limit",
            "--time-in-force",
            "day",
            "--order-class",
            "mleg",
            "--qty",
            str(quantity),
            "--limit-price",
            f"{limit_price:.2f}",
            "--legs",
            json.dumps(legs, separators=(",", ":")),
            "--client-order-id",
            client_order_id,
        ]
