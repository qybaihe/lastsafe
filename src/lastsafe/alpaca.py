from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
from datetime import UTC, date, datetime, timedelta

import httpx

from .config import Settings
from .engine import ParsedOptionSymbol, parse_occ_symbol
from .models import (
    AccountSnapshot,
    DeskSnapshot,
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
        account_raw, positions_raw, clock_raw, history_raw = await asyncio.gather(
            self._get(f"{self.trading_base}/v2/account"),
            self._get(f"{self.trading_base}/v2/positions"),
            self._get(f"{self.trading_base}/v2/clock"),
            self._get(
                f"{self.trading_base}/v2/account/portfolio/history",
                params={"period": "1W", "timeframe": "1H", "intraday_reporting": "market_hours"},
            ),
        )
        assert isinstance(account_raw, dict)
        assert isinstance(positions_raw, list)
        assert isinstance(clock_raw, dict)
        assert isinstance(history_raw, dict)

        spread = await self._select_vertical(positions_raw)
        symbols = f"{spread.long_leg.symbol},{spread.short_leg.symbol}"
        quotes_raw = await self._get(
            f"{self.data_base}/v1beta1/options/quotes/latest",
            params={"symbols": symbols, "feed": self.settings.alpaca_data_feed},
        )
        assert isinstance(quotes_raw, dict)
        quote_map = quotes_raw.get("quotes", {})
        spread.long_leg.quote = self._quote(quote_map.get(spread.long_leg.symbol))
        spread.short_leg.quote = self._quote(quote_map.get(spread.short_leg.symbol))

        stock_snapshot = await self._get(
            f"{self.data_base}/v2/stocks/{spread.underlying}/snapshot",
            params={"feed": "iex"},
        )
        assert isinstance(stock_snapshot, dict)
        latest_trade = stock_snapshot.get("latestTrade") or {}
        spot = float(latest_trade.get("p") or stock_snapshot.get("dailyBar", {}).get("c") or 0)
        if spot <= 0:
            raise AlpacaError("Underlying spot price is unavailable")
        roll = await self._find_roll(spread, spot)

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
                options_buying_power=float(
                    account_raw.get("options_buying_power") or account_raw["buying_power"]
                ),
                options_trading_level=int(account_raw.get("options_trading_level") or 0),
                daily_pnl=daily_pnl,
                total_pnl=equity - starting_equity,
                max_drawdown_pct=self._max_drawdown(curve),
                paper=True,
            ),
            position=spread,
            roll_candidate=roll,
            equity_curve=curve,
        )

    async def _select_vertical(self, positions: list[dict]) -> SpreadPosition:
        option_positions: list[tuple[dict, ParsedOptionSymbol]] = []
        for position in positions:
            symbol = str(position.get("symbol", ""))
            try:
                parsed = parse_occ_symbol(symbol)
            except ValueError:
                continue
            option_positions.append((position, parsed))

        groups: dict[tuple[str, date, str], list[tuple[dict, ParsedOptionSymbol]]] = {}
        for item in option_positions:
            position, parsed = item
            key = (parsed.underlying, parsed.expiration, parsed.option_type)
            groups.setdefault(key, []).append(item)

        for (underlying, expiration, option_type), items in sorted(
            groups.items(), key=lambda group: group[0][1]
        ):
            long_items = [item for item in items if float(item[0]["qty"]) > 0]
            short_items = [item for item in items if float(item[0]["qty"]) < 0]
            for long_item in long_items:
                for short_item in short_items:
                    long_position, long_parsed = long_item
                    short_position, short_parsed = short_item
                    quantity = min(
                        abs(int(float(long_position["qty"]))),
                        abs(int(float(short_position["qty"]))),
                    )
                    if quantity < 1:
                        continue
                    strategy = (
                        "bull_put_credit"
                        if option_type == "put" and short_parsed.strike > long_parsed.strike
                        else "bear_call_credit"
                        if option_type == "call" and short_parsed.strike < long_parsed.strike
                        else None
                    )
                    if strategy is None:
                        continue
                    long_price = float(long_position.get("avg_entry_price") or 0)
                    short_price = float(short_position.get("avg_entry_price") or 0)
                    entry_credit = short_price - long_price
                    if entry_credit <= 0:
                        continue
                    placeholder = Quote(bid=0, ask=0, timestamp=datetime.now(UTC))
                    return SpreadPosition(
                        id=f"{underlying}-{expiration}-{short_parsed.strike}-{long_parsed.strike}",
                        underlying=underlying,
                        strategy=strategy,
                        quantity=quantity,
                        opened_at=datetime.now(UTC),
                        entry_credit=entry_credit,
                        long_leg=OptionLeg(
                            symbol=str(long_position["symbol"]),
                            position_side="long",
                            option_type=option_type,
                            strike=long_parsed.strike,
                            expiration=expiration,
                            quantity=quantity,
                            entry_price=long_price,
                            quote=placeholder,
                        ),
                        short_leg=OptionLeg(
                            symbol=str(short_position["symbol"]),
                            position_side="short",
                            option_type=option_type,
                            strike=short_parsed.strike,
                            expiration=expiration,
                            quantity=quantity,
                            entry_price=short_price,
                            quote=placeholder,
                        ),
                    )
        raise AlpacaError("No supported paired vertical spread was found in the paper account")

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
        values = [float(value) for value in history.get("equity", []) if value is not None]
        return values[0] if values else fallback

    @staticmethod
    def _max_drawdown(curve: list[EquityPoint]) -> float:
        peak = curve[0].equity
        drawdown = 0.0
        for point in curve:
            peak = max(peak, point.equity)
            drawdown = min(drawdown, (point.equity - peak) / peak * 100)
        return round(drawdown, 3)

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        normalized = value.rstrip("Z")
        if "." in normalized:
            prefix, fractional = normalized.split(".", 1)
            normalized = f"{prefix}.{fractional[:6]}"
        return datetime.fromisoformat(f"{normalized}+00:00")

    async def execute_via_cli(
        self, action: str, snapshot: DeskSnapshot, client_order_id: str
    ) -> tuple[str, dict, str, bool]:
        if action == "HOLD":
            return "", {}, "HOLD never creates an order", False
        args = self._cli_args(action, snapshot, client_order_id)
        existing = await self._query_cli_order(client_order_id)
        if existing is not None:
            return (
                self.command_preview(action, snapshot, client_order_id),
                existing,
                "Existing Alpaca paper order recovered by stable client order ID",
                True,
            )
        payload = await self._run_cli(args, allow_not_found=False)
        if payload is None:
            raise AlpacaError("Alpaca CLI did not return an order")
        reconciled = await self._query_cli_order(client_order_id)
        if reconciled is not None:
            payload = reconciled
        return (
            self.command_preview(action, snapshot, client_order_id),
            payload,
            "Order submitted and reconciled through Alpaca CLI in paper mode",
            False,
        )

    async def _query_cli_order(self, client_order_id: str) -> dict | None:
        return await self._run_cli(
            [
                self.settings.alpaca_cli_path,
                "order",
                "get-by-client-id",
                "--client-order-id",
                client_order_id,
            ],
            allow_not_found=True,
        )

    async def _run_cli(self, args: list[str], *, allow_not_found: bool) -> dict | None:
        env = {
            **os.environ,
            "ALPACA_API_KEY": self.settings.alpaca_api_key,
            "ALPACA_SECRET_KEY": self.settings.alpaca_secret_key,
            "ALPACA_LIVE_TRADE": "false",
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
        try:
            payload = json.loads(stdout.decode())
        except json.JSONDecodeError as error:
            raise AlpacaError("Alpaca CLI returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise AlpacaError("Alpaca CLI returned an unexpected response")
        return payload

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
    def client_order_id(action: str, snapshot: DeskSnapshot) -> str:
        roll_symbols = ""
        if action == "ROLL" and snapshot.roll_candidate is not None:
            roll_symbols = (
                f"-{snapshot.roll_candidate.long_leg.symbol}"
                f"-{snapshot.roll_candidate.short_leg.symbol}"
            )
        identity = (
            f"{snapshot.account.account_id}-{snapshot.position.id}-{action}{roll_symbols}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return f"lastsafe-{action.lower()}-{digest}"

    def command_preview(self, action: str, snapshot: DeskSnapshot, client_order_id: str) -> str:
        args = self._cli_args(action, snapshot, client_order_id)
        return shlex.join(args)

    def _cli_args(self, action: str, snapshot: DeskSnapshot, client_order_id: str) -> list[str]:
        position = snapshot.position
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
            str(position.quantity),
            "--limit-price",
            f"{limit_price:.2f}",
            "--legs",
            json.dumps(legs, separators=(",", ":")),
            "--client-order-id",
            client_order_id,
        ]
