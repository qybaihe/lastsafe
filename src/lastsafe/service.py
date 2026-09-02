from __future__ import annotations

import asyncio
import hashlib
import uuid
from asyncio import Lock
from contextlib import suppress
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .alpaca import AlpacaClient, AlpacaError
from .config import Settings
from .engine import evaluate
from .evidence import attribution_for, counterfactual_for, write_packet
from .llm import DecisionService
from .models import (
    Action,
    AgentDecision,
    BootstrapResponse,
    DeskSnapshot,
    Evaluation,
    ExecutionReceipt,
    PendingIntent,
    RunRecord,
    RunRequest,
    ScenarioRequest,
)
from .replay import load_replay_snapshot
from .store import RunStore


class LastSafeService:
    def __init__(self, settings: Settings, store: RunStore):
        self.settings = settings
        self.store = store
        self.alpaca = AlpacaClient(settings)
        self.decisions = DecisionService(settings)
        self._run_lock = Lock()

    async def get_snapshot(self) -> DeskSnapshot:
        if self.settings.mode == "replay":
            return load_replay_snapshot()
        snapshot = await self.alpaca.snapshot()
        enrollment = self.store.get_metadata("competition_account")
        if isinstance(enrollment, dict) and enrollment.get("account_id") == (
            snapshot.account.account_id
        ):
            snapshot.account.starting_equity = float(enrollment["equity"])
            snapshot.account.starting_equity_verified = True
            snapshot.account.total_pnl = round(
                snapshot.account.equity - snapshot.account.starting_equity, 2
            )
            competition_curve = [
                point
                for point in snapshot.equity_curve
                if point.timestamp
                >= datetime.fromisoformat(
                    str(enrollment["captured_at"]).replace("Z", "+00:00")
                )
            ]
            if competition_curve:
                snapshot.account.max_drawdown_pct = self._curve_drawdown(
                    snapshot.account.starting_equity, competition_curve
                )
        else:
            snapshot.account.starting_equity_verified = False
        return snapshot

    @staticmethod
    def _curve_drawdown(starting_equity: float, points) -> float:
        peak = starting_equity
        drawdown = 0.0
        for point in points:
            if point.equity <= 0:
                continue
            peak = max(peak, point.equity)
            drawdown = min(drawdown, (point.equity - peak) / peak * 100)
        return round(drawdown, 3)

    async def enroll_competition_account(self) -> dict:
        if self.settings.mode != "alpaca":
            raise AlpacaError("Competition enrollment requires Alpaca mode")
        preflight = await self.alpaca.competition_preflight()
        existing = self.store.get_metadata("competition_account")
        if isinstance(existing, dict):
            if existing.get("account_id") != preflight["account_id"]:
                raise AlpacaError(
                    "This database is already bound to a different competition account"
                )
            if existing != preflight:
                raise AlpacaError("Competition account enrollment is immutable once recorded")
            return existing
        self.store.set_metadata("competition_account", preflight)
        return preflight

    async def bootstrap(self, scenario: ScenarioRequest | None = None) -> BootstrapResponse:
        snapshot = await self.get_snapshot()
        if scenario is None:
            scenario = ScenarioRequest(
                minutes_to_close=min(max(snapshot.minutes_to_close, 5), 390),
                as_of_date=snapshot.as_of.date(),
            )
        elif self.settings.mode == "alpaca":
            scenario.minutes_to_close = min(max(snapshot.minutes_to_close, 5), 390)
        if scenario.as_of_date is None:
            scenario.as_of_date = snapshot.as_of.astimezone(
                ZoneInfo("America/New_York")
            ).date()
        evaluation = evaluate(snapshot, scenario)
        chain_valid, chain_length = self.store.verify()
        return BootstrapResponse(
            snapshot=snapshot,
            evaluation=evaluation,
            latest_run=self.store.latest(),
            capabilities={
                "mode": self.settings.mode,
                "paper_only": True,
                "execution_enabled": self.settings.execution_enabled,
                "llm_configured": bool(self.settings.llm_api_key),
                "alpaca_configured": bool(
                    self.settings.alpaca_api_key and self.settings.alpaca_secret_key
                ),
                "audit_chain_valid": chain_valid,
                "audit_chain_length": str(chain_length),
            },
        )

    async def run(
        self, request: RunRequest, *, trigger: str = "manual"
    ) -> RunRecord:
        async with self._run_lock:
            owner = f"api:{uuid.uuid4()}"
            lease = (
                self.store.acquire_lease(owner, ttl_seconds=self._lease_ttl())
                if request.execute
                else None
            )
            if request.execute and lease is None:
                raise AlpacaError("Another execution cycle owns the broker lease")
            renewer = (
                asyncio.create_task(self._renew_lease(lease)) if lease is not None else None
            )
            try:
                return await self._run_locked(request, trigger=trigger, lease_token=lease)
            finally:
                if renewer:
                    renewer.cancel()
                    with suppress(asyncio.CancelledError):
                        await renewer
                if lease:
                    self.store.release_lease(lease)

    def _lease_ttl(self) -> int:
        return self.settings.order_timeout_seconds + int(
            self.settings.order_poll_seconds * 8
        ) + 300

    async def _renew_lease(self, token: str) -> None:
        interval = max(10, self._lease_ttl() // 3)
        while True:
            await asyncio.sleep(interval)
            if not self.store.renew_lease(token, ttl_seconds=self._lease_ttl()):
                raise AlpacaError("Execution lease was lost")

    async def _run_locked(
        self,
        request: RunRequest,
        *,
        trigger: str,
        lease_token: str | None = None,
    ) -> RunRecord:
        if request.execute:
            chain_valid, _ = self.store.verify()
            if not chain_valid:
                raise AlpacaError("Audit chain is invalid; execution is blocked")
            pending = self.store.list_pending_intents()
            if pending:
                raise AlpacaError(
                    "Pending broker intent must be reconciled before a new execution"
                )
        snapshot = await self.get_snapshot()
        if self.settings.mode == "alpaca" and request.execute:
            active_scenario = ScenarioRequest(
                spot_shift_pct=0,
                buying_power_pct=100,
                minutes_to_close=min(max(snapshot.minutes_to_close, 5), 390),
                as_of_date=snapshot.as_of.astimezone(
                    ZoneInfo("America/New_York")
                ).date(),
            )
        else:
            active_scenario = request.scenario.model_copy(deep=True)
        if active_scenario.as_of_date is None:
            active_scenario.as_of_date = snapshot.as_of.astimezone(
                ZoneInfo("America/New_York")
            ).date()
        evaluation = evaluate(snapshot, active_scenario)
        decision = await self.decisions.decide(snapshot, evaluation)
        execution = await self._execute(
            decision,
            snapshot,
            evaluation,
            request.execute,
            lease_token=lease_token,
        )
        action = Action(decision.action)
        evidence_snapshot = snapshot
        if (
            self.settings.mode == "alpaca"
            and execution.position_verified
            and action in {Action.OPEN, Action.CLOSE}
        ):
            try:
                post_snapshot = await self.get_snapshot()
                snapshot.account.equity = post_snapshot.account.equity
                snapshot.account.total_pnl = post_snapshot.account.total_pnl
                snapshot.account.max_drawdown_pct = post_snapshot.account.max_drawdown_pct
                snapshot.equity_curve = post_snapshot.equity_curve
                evidence_snapshot = snapshot
            except AlpacaError:
                pass
        attribution = attribution_for(evidence_snapshot, action, execution)
        counterfactual = counterfactual_for(evidence_snapshot, action, attribution)
        latest = self.store.latest()
        previous_hash = latest.record_hash if latest else "GENESIS"
        record = RunRecord(
            id=f"run_{uuid.uuid4().hex[:12]}",
            created_at=datetime.now(UTC),
            mode=self.settings.mode,
            snapshot=snapshot,
            evaluation=evaluation,
            decision=decision,
            execution=execution,
            attribution=attribution,
            counterfactual=counterfactual,
            trigger=trigger,
            previous_hash=previous_hash,
            record_hash="pending",
        )
        record.record_hash = self.store.hash_record(record)
        record = self.store.append(record)
        enrollment = self.store.get_metadata("competition_account")
        write_packet(
            record,
            self.settings,
            enrollment if isinstance(enrollment, dict) else None,
        )
        return record

    async def _execute(
        self,
        decision: AgentDecision,
        snapshot: DeskSnapshot,
        evaluation: Evaluation,
        requested: bool,
        lease_token: str | None,
    ) -> ExecutionReceipt:
        action = Action(decision.action)
        now = datetime.now(UTC)
        if not requested or action in {Action.HOLD, Action.STAND_DOWN}:
            return ExecutionReceipt(
                status="not-requested",
                action=action,
                detail=(
                    "No order requested; decision is recorded for audit."
                    if not requested
                    else f"{action.value} is an autonomous no-order action."
                ),
            )

        incident_key = self.alpaca.incident_key(action.value, snapshot)
        attempt = self.store.current_attempt(incident_key)
        client_order_id = self.alpaca.client_order_id(
            action.value, snapshot, attempt=attempt
        )
        preview = self.alpaca.command_preview(action.value, snapshot, client_order_id)
        if self.settings.mode == "replay":
            return ExecutionReceipt(
                status="simulated",
                action=action,
                client_order_id=client_order_id,
                order_id=f"REPLAY-{uuid.uuid4().hex[:8].upper()}",
                command_preview=preview,
                submitted_at=now,
                broker_status="accepted (replay)",
                detail="Replay simulated the exact paper CLI payload; no broker request was sent.",
            )
        if not self.settings.execution_enabled:
            return ExecutionReceipt(
                status="blocked",
                action=action,
                client_order_id=client_order_id,
                command_preview=preview,
                detail="Execution is locked by LASTSAFE_EXECUTION_ENABLED=false.",
            )
        if lease_token is None or not self.store.owns_lease(lease_token):
            return ExecutionReceipt(
                status="blocked",
                action=action,
                client_order_id=client_order_id,
                command_preview=preview,
                detail="Execution lease is missing or expired.",
            )
        allowed = {
            Action(outcome.action) for outcome in evaluation.outcomes if outcome.allowed
        }
        if action not in allowed:
            return ExecutionReceipt(
                status="blocked",
                action=action,
                client_order_id=client_order_id,
                command_preview=preview,
                detail="A fresh pre-trade evaluation no longer permits this action.",
            )
        try:
            # The model may take time. Re-read broker truth immediately before mutation.
            fresh_snapshot = await self.get_snapshot()
            fresh_scenario = ScenarioRequest(
                minutes_to_close=min(max(fresh_snapshot.minutes_to_close, 5), 390),
                as_of_date=fresh_snapshot.as_of.astimezone(
                    ZoneInfo("America/New_York")
                ).date(),
            )
            fresh_evaluation = evaluate(fresh_snapshot, fresh_scenario)
            fresh_allowed = {
                Action(outcome.action)
                for outcome in fresh_evaluation.outcomes
                if outcome.allowed
            }
            if action not in fresh_allowed or not self._same_trade(
                action, snapshot, fresh_snapshot
            ):
                return ExecutionReceipt(
                    status="blocked",
                    action=action,
                    client_order_id=client_order_id,
                    command_preview=preview,
                    detail="Fresh broker snapshot changed the legal action or contract set.",
                )
            client_order_id = self.alpaca.client_order_id(
                action.value, fresh_snapshot, attempt=attempt
            )
            preview = self.alpaca.command_preview(
                action.value, fresh_snapshot, client_order_id
            )
            request_hash = hashlib.sha256(preview.encode()).hexdigest()
            intent = PendingIntent(
                incident_key=incident_key,
                action=action,
                attempt=attempt,
                client_order_id=client_order_id,
                created_at=datetime.now(UTC),
                request_hash=request_hash,
                command_preview=preview,
                expected_positions=self._expected_positions(action, fresh_snapshot),
                draft_run={
                    "snapshot": fresh_snapshot.model_dump(mode="json"),
                    "evaluation": fresh_evaluation.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "trigger": "recovery",
                },
                state="submitting",
            )
            self.store.save_intent(intent)
            if not self.store.renew_lease(lease_token, ttl_seconds=self._lease_ttl()):
                raise AlpacaError("Execution lease was lost before broker mutation")

            async def prepare_submission() -> DeskSnapshot:
                boundary = await self.get_snapshot()
                boundary_scenario = ScenarioRequest(
                    minutes_to_close=min(max(boundary.minutes_to_close, 5), 390),
                    as_of_date=boundary.as_of.astimezone(
                        ZoneInfo("America/New_York")
                    ).date(),
                )
                boundary_evaluation = evaluate(boundary, boundary_scenario)
                boundary_allowed = {
                    Action(outcome.action)
                    for outcome in boundary_evaluation.outcomes
                    if outcome.allowed
                }
                if action not in boundary_allowed or not self._same_trade(
                    action, fresh_snapshot, boundary
                ):
                    raise AlpacaError(
                        "Final quote/session boundary changed before submission"
                    )
                boundary_preview = self.alpaca.command_preview(
                    action.value, boundary, client_order_id
                )
                intent.request_hash = hashlib.sha256(
                    boundary_preview.encode()
                ).hexdigest()
                intent.command_preview = boundary_preview
                intent.expected_positions = self._expected_positions(action, boundary)
                self.store.save_intent(intent)
                return boundary

            command, payload, detail, recovered, lifecycle = (
                await self.alpaca.execute_via_cli(
                    action.value,
                    fresh_snapshot,
                    client_order_id,
                    attempt=attempt,
                    mutation_guard=lambda: self.store.owns_lease(lease_token),
                    prepare_submission=prepare_submission,
                )
            )
            broker_status = self.alpaca._status(payload)
            partially_filled = self._optional_float(payload.get("filled_qty")) or 0
            intent.state = (
                "observed"
                if broker_status in {"filled", "replaced", "calculated"}
                or partially_filled > 0
                else "terminal"
                if broker_status in self.alpaca._terminal_statuses()
                else "observed"
            )
            intent.broker_order_id = str(payload.get("id") or "") or None
            intent.broker_status = broker_status
            self.store.save_intent(intent)
            position_verified = (
                await self._verify_positions(action, fresh_snapshot)
                if broker_status == "filled"
                else False
            )
            if broker_status in self.alpaca._terminal_statuses() and partially_filled > 0:
                position_verified = self._positions_match(
                    action,
                    fresh_snapshot,
                    {
                        str(row.get("symbol")): float(row.get("qty") or 0)
                        for row in await self.alpaca.positions_via_cli()
                    },
                )
            status = self._receipt_status(broker_status, position_verified, recovered)
            if broker_status == "filled" and position_verified:
                intent.state = "terminal"
                self.store.save_intent(intent)
            receipt = ExecutionReceipt(
                status=status,
                action=action,
                client_order_id=client_order_id,
                order_id=str(payload.get("id", "")) or None,
                command_preview=command,
                submitted_at=now,
                broker_status=broker_status,
                detail=detail,
                raw=payload,
                terminal_at=(
                    datetime.now(UTC)
                    if broker_status in self.alpaca._terminal_statuses()
                    else None
                ),
                filled_qty=self._optional_float(payload.get("filled_qty")),
                filled_avg_price=self._optional_float(payload.get("filled_avg_price")),
                position_verified=position_verified,
                lifecycle=lifecycle,
                attempt=attempt,
            )
            if receipt.status in {"canceled", "rejected", "expired"}:
                if receipt.filled_qty in {None, 0}:
                    self.store.advance_attempt(incident_key)
                else:
                    intent.state = "observed"
                    self.store.save_intent(intent)
            return receipt
        except AlpacaError as error:
            return ExecutionReceipt(
                status="failed",
                action=action,
                client_order_id=client_order_id,
                command_preview=preview,
                submitted_at=now,
                detail=str(error),
            )

    @staticmethod
    def _same_trade(action: Action, old: DeskSnapshot, new: DeskSnapshot) -> bool:
        if action == Action.OPEN:
            return bool(
                old.entry_candidate
                and new.entry_candidate
                and old.entry_candidate.long_leg.symbol
                == new.entry_candidate.long_leg.symbol
                and old.entry_candidate.short_leg.symbol
                == new.entry_candidate.short_leg.symbol
            )
        if not old.position or not new.position:
            return False
        if (
            old.position.long_leg.symbol != new.position.long_leg.symbol
            or old.position.short_leg.symbol != new.position.short_leg.symbol
        ):
            return False
        if action == Action.ROLL:
            return bool(
                old.roll_candidate
                and new.roll_candidate
                and old.roll_candidate.long_leg.symbol == new.roll_candidate.long_leg.symbol
                and old.roll_candidate.short_leg.symbol
                == new.roll_candidate.short_leg.symbol
            )
        return True

    @staticmethod
    def _expected_positions(action: Action, snapshot: DeskSnapshot) -> dict[str, float]:
        if action == Action.OPEN and snapshot.entry_candidate:
            return {
                snapshot.entry_candidate.long_leg.symbol: snapshot.entry_candidate.quantity,
                snapshot.entry_candidate.short_leg.symbol: -snapshot.entry_candidate.quantity,
            }
        if action == Action.ROLL and snapshot.roll_candidate:
            return {
                snapshot.roll_candidate.long_leg.symbol: snapshot.roll_candidate.quantity,
                snapshot.roll_candidate.short_leg.symbol: -snapshot.roll_candidate.quantity,
            }
        return {}

    async def _verify_positions(self, action: Action, before: DeskSnapshot) -> bool:
        if self.settings.mode != "alpaca":
            return False
        for _ in range(5):
            rows = await self.alpaca.positions_via_cli()
            quantities = {
                str(row.get("symbol")): float(row.get("qty") or 0) for row in rows
            }
            verified = self._positions_match(action, before, quantities)
            if verified:
                return True
            await asyncio.sleep(self.settings.order_poll_seconds)
        return False

    @staticmethod
    def _positions_match(
        action: Action, before: DeskSnapshot, quantities: dict[str, float]
    ) -> bool:
        if action == Action.OPEN:
            candidate = before.entry_candidate
            return bool(
                candidate
                and quantities.get(candidate.long_leg.symbol) == candidate.quantity
                and quantities.get(candidate.short_leg.symbol) == -candidate.quantity
                and all(
                    symbol in {candidate.long_leg.symbol, candidate.short_leg.symbol}
                    or quantity == 0
                    for symbol, quantity in quantities.items()
                )
            )
        position = before.position
        if position is None:
            return False
        old_flat = quantities.get(position.long_leg.symbol, 0) == 0 and quantities.get(
            position.short_leg.symbol, 0
        ) == 0
        if action == Action.CLOSE:
            return old_flat and not any(value != 0 for value in quantities.values())
        roll = before.roll_candidate
        return bool(
            old_flat
            and roll
            and quantities.get(roll.long_leg.symbol) == roll.quantity
            and quantities.get(roll.short_leg.symbol) == -roll.quantity
            and all(
                symbol in {roll.long_leg.symbol, roll.short_leg.symbol} or quantity == 0
                for symbol, quantity in quantities.items()
            )
        )

    @staticmethod
    def _receipt_status(
        broker_status: str, position_verified: bool, recovered: bool
    ) -> str:
        if broker_status == "filled":
            return "filled" if position_verified else "working"
        if broker_status in {"canceled", "cancelled"}:
            return "canceled"
        if broker_status in {"rejected", "suspended"}:
            return "rejected"
        if broker_status in {"expired", "done_for_day"}:
            return "expired"
        if broker_status in {"replaced", "calculated"}:
            return "working"
        if broker_status == "unknown":
            return "unknown"
        return "recovered" if recovered else "working"

    @staticmethod
    def _optional_float(value) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
