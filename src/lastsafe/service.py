from __future__ import annotations

import uuid
from asyncio import Lock
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .alpaca import AlpacaClient, AlpacaError
from .config import Settings
from .engine import evaluate
from .llm import DecisionService
from .models import (
    Action,
    AgentDecision,
    BootstrapResponse,
    DeskSnapshot,
    Evaluation,
    ExecutionReceipt,
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
        return await self.alpaca.snapshot()

    async def bootstrap(self, scenario: ScenarioRequest | None = None) -> BootstrapResponse:
        snapshot = await self.get_snapshot()
        scenario = scenario or ScenarioRequest(
            minutes_to_close=max(snapshot.minutes_to_close, 5),
            as_of_date=snapshot.as_of.date(),
        )
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

    async def run(self, request: RunRequest) -> RunRecord:
        async with self._run_lock:
            return await self._run_locked(request)

    async def _run_locked(self, request: RunRequest) -> RunRecord:
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
        execution = await self._execute(decision, snapshot, evaluation, request.execute)
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
            previous_hash=previous_hash,
            record_hash="pending",
        )
        record.record_hash = self.store.hash_record(record)
        return self.store.append(record)

    async def _execute(
        self,
        decision: AgentDecision,
        snapshot: DeskSnapshot,
        evaluation: Evaluation,
        requested: bool,
    ) -> ExecutionReceipt:
        action = Action(decision.action)
        now = datetime.now(UTC)
        if not requested or action == Action.HOLD:
            return ExecutionReceipt(
                status="not-requested",
                action=action,
                detail=(
                    "No order requested; decision is recorded for audit."
                    if not requested
                    else "HOLD is an autonomous no-order action."
                ),
            )

        client_order_id = self.alpaca.client_order_id(action.value, snapshot)
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
            command, payload, detail, recovered = await self.alpaca.execute_via_cli(
                action.value, snapshot, client_order_id
            )
            return ExecutionReceipt(
                status="recovered" if recovered else "submitted",
                action=action,
                client_order_id=client_order_id,
                order_id=str(payload.get("id", "")) or None,
                command_preview=command,
                submitted_at=now,
                broker_status=str(payload.get("status", "submitted")),
                detail=detail,
                raw=payload,
            )
        except AlpacaError as error:
            return ExecutionReceipt(
                status="failed",
                action=action,
                client_order_id=client_order_id,
                command_preview=preview,
                submitted_at=now,
                detail=str(error),
            )
