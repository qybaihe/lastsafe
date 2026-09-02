from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from .config import Settings
from .evidence import attribution_for, counterfactual_for, write_packet
from .models import (
    AgentDecision,
    DeskSnapshot,
    Evaluation,
    ExecutionReceipt,
    RunRecord,
    RunRequest,
    ScenarioRequest,
    WorkerHeartbeat,
)
from .service import LastSafeService
from .store import RunStore


class LastSafeWorker:
    def __init__(self, settings: Settings, service: LastSafeService, store: RunStore):
        self.settings = settings
        self.service = service
        self.store = store
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
        self.stopping = False

    async def cycle(self) -> WorkerHeartbeat:
        lease = self.store.acquire_lease(
            self.owner, ttl_seconds=self.service._lease_ttl()
        )
        if lease is None:
            return self._heartbeat("degraded", error="another worker owns the lease")
        self._heartbeat("running")
        try:
            recovered_run = False
            pending = self.store.list_pending_intents()
            if pending:
                unresolved = []
                for intent in pending:
                    order = await self.service.alpaca._query_cli_order(
                        intent.client_order_id
                    )
                    if order is None:
                        unresolved.append(intent.client_order_id)
                        continue
                    intent.broker_order_id = str(order.get("id") or "") or None
                    intent.broker_status = self.service.alpaca._status(order)
                    filled_qty = self.service._optional_float(order.get("filled_qty")) or 0
                    if intent.broker_status == "filled":
                        positions = await self.service.alpaca.positions_via_cli()
                        quantities = {
                            str(row.get("symbol")): float(row.get("qty") or 0)
                            for row in positions
                        }
                        if quantities != intent.expected_positions:
                            unresolved.append(intent.client_order_id)
                            intent.state = "observed"
                            self.store.save_intent(intent)
                            continue
                        self._complete_recovered_intent(intent, order)
                        recovered_run = True
                        continue
                    if intent.broker_status in {"replaced", "calculated"}:
                        unresolved.append(intent.client_order_id)
                        intent.state = "observed"
                        self.store.save_intent(intent)
                        continue
                    if filled_qty > 0:
                        unresolved.append(intent.client_order_id)
                        intent.state = "observed"
                        self.store.save_intent(intent)
                        continue
                    intent.state = (
                        "terminal"
                        if intent.broker_status
                        in self.service.alpaca._terminal_statuses()
                        else "observed"
                    )
                    self.store.save_intent(intent)
                    if intent.state == "terminal" and intent.broker_status in {
                        "canceled",
                        "cancelled",
                        "expired",
                        "rejected",
                        "suspended",
                        "done_for_day",
                    }:
                        self.store.advance_attempt(intent.incident_key)
                    if intent.state != "terminal":
                        unresolved.append(intent.client_order_id)
                if unresolved:
                    return self._heartbeat(
                        "degraded",
                        error=(
                            "pending broker intent requires reconciliation: "
                            + ", ".join(unresolved)
                        ),
                    )
            if recovered_run:
                latest = self.store.latest()
                return self._heartbeat(
                    "healthy",
                    run_id=latest.id if latest else None,
                    action=str(latest.decision.action) if latest else None,
                )
            record = await self.service._run_locked(
                RunRequest(
                    scenario=ScenarioRequest(),
                    execute=self.settings.execution_enabled,
                ),
                trigger="scheduler",
                lease_token=lease,
            )
            return self._heartbeat(
                "healthy", run_id=record.id, action=str(record.decision.action)
            )
        except Exception as error:
            return self._heartbeat("error", error=f"{type(error).__name__}: {error}")
        finally:
            self.store.release_lease(lease)

    def _complete_recovered_intent(self, intent, order: dict) -> RunRecord:
        snapshot = DeskSnapshot.model_validate(intent.draft_run["snapshot"])
        evaluation = Evaluation.model_validate(intent.draft_run["evaluation"])
        decision = AgentDecision.model_validate(intent.draft_run["decision"])
        receipt = ExecutionReceipt(
            status="filled",
            action=intent.action,
            client_order_id=intent.client_order_id,
            order_id=str(order.get("id") or "") or None,
            command_preview=intent.command_preview,
            submitted_at=intent.created_at,
            terminal_at=datetime.now(UTC),
            broker_status="filled",
            detail="Recovered broker fill and verified exact resulting positions after restart.",
            raw=order,
            attempt=intent.attempt,
            filled_qty=self.service._optional_float(order.get("filled_qty")),
            filled_avg_price=self.service._optional_float(order.get("filled_avg_price")),
            position_verified=True,
            lifecycle=[
                {
                    "at": datetime.now(UTC).isoformat(),
                    "event": "restart_recovery",
                    "status": "filled",
                }
            ],
        )
        attribution = attribution_for(snapshot, intent.action, receipt)
        counterfactual = counterfactual_for(snapshot, intent.action, attribution)
        latest = self.store.latest()
        record = RunRecord(
            id=f"run_recovered_{uuid.uuid4().hex[:12]}",
            created_at=datetime.now(UTC),
            mode=self.settings.mode,
            snapshot=snapshot,
            evaluation=evaluation,
            decision=decision,
            execution=receipt,
            attribution=attribution,
            counterfactual=counterfactual,
            trigger="recovery",
            previous_hash=latest.record_hash if latest else "GENESIS",
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
        intent.state = "terminal"
        self.store.save_intent(intent)
        return record

    async def run_forever(self) -> None:
        while not self.stopping:
            await self.cycle()
            await asyncio.sleep(self.settings.worker_interval_seconds)
        self._heartbeat("stopped")

    def stop(self) -> None:
        self.stopping = True

    def _heartbeat(
        self,
        status: str,
        *,
        run_id: str | None = None,
        action: str | None = None,
        error: str | None = None,
    ) -> WorkerHeartbeat:
        now = datetime.now(UTC)
        heartbeat = WorkerHeartbeat(
            status=status,
            updated_at=now,
            owner=self.owner,
            last_run_id=run_id,
            last_action=action,
            last_error=error,
            next_run_at=(
                now + timedelta(seconds=self.settings.worker_interval_seconds)
                if status in {"healthy", "degraded", "error"}
                else None
            ),
        )
        self.store.set_metadata("worker_heartbeat", heartbeat.model_dump(mode="json"))
        return heartbeat


async def _run(once: bool, enroll: bool) -> None:
    settings = Settings.from_env()
    settings.validate_runtime()
    store = RunStore(settings.database_path)
    service = LastSafeService(settings, store)
    worker = LastSafeWorker(settings, service, store)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, worker.stop)
    try:
        heartbeat = None
        if enroll:
            await service.enroll_competition_account()
        elif once:
            heartbeat = await worker.cycle()
        else:
            await worker.run_forever()
        if heartbeat is not None and heartbeat.status in {"degraded", "error"}:
            raise SystemExit(1)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="LastSafe autonomous lifecycle worker")
    parser.add_argument("--once", action="store_true", help="run exactly one broker cycle")
    parser.add_argument(
        "--enroll-account",
        action="store_true",
        help="bind a pristine $100K competition account before any trading",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.once, args.enroll_account))


if __name__ == "__main__":
    main()
