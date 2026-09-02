from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .models import (
    Action,
    CounterfactualResult,
    DeskSnapshot,
    EvidencePacket,
    ExecutionReceipt,
    PnlAttribution,
    RunRecord,
)

DISCLOSURE = (
    "Alpaca paper trading only. Replay and modeled counterfactuals are hypothetical and do "
    "not establish future performance. Not investment advice."
)


def attribution_for(
    snapshot: DeskSnapshot, action: Action, execution: ExecutionReceipt
) -> PnlAttribution | None:
    position = snapshot.position
    if position is None:
        open_credit = (
            round(
                snapshot.entry_candidate.open_credit
                * 100
                * snapshot.entry_candidate.quantity,
                2,
            )
            if snapshot.entry_candidate
            and execution.position_verified
            and action == Action.OPEN
            else None
        )
        return PnlAttribution(
            basis_credit=0,
            new_spread_open_credit=open_credit,
            account_total_pnl=snapshot.account.total_pnl,
            other_account_movement=snapshot.account.total_pnl,
        )
    fill_price = execution.filled_avg_price
    close_debit = fill_price if fill_price is not None and action == Action.CLOSE else None
    if (
        close_debit is None
        and action in {Action.CLOSE, Action.ROLL}
        and execution.status == "simulated"
    ):
        close_debit = max(
            position.short_leg.quote.ask - position.long_leg.quote.bid, 0
        )
    realized = (
        round((position.entry_credit - close_debit) * 100 * position.quantity, 2)
        if close_debit is not None
        and (execution.status == "simulated" or execution.position_verified)
        else None
    )
    if action == Action.ROLL and execution.position_verified:
        # Alpaca's parent fill is the signed net roll price, not the old close component.
        # Exact old/new attribution requires child-leg fills or FILL activities.
        close_debit = None
        realized = None
    return PnlAttribution(
        basis_credit=round(position.entry_credit * 100 * position.quantity, 2),
        close_debit=(
            round(close_debit * 100 * position.quantity, 2)
            if close_debit is not None
            else None
        ),
        old_spread_realized_pnl=realized,
        new_spread_open_credit=(
            round(snapshot.roll_candidate.open_credit * 100 * position.quantity, 2)
            if (
                action == Action.ROLL
                and snapshot.roll_candidate
                and execution.status == "simulated"
            )
            else None
        ),
        account_total_pnl=snapshot.account.total_pnl,
        other_account_movement=(
            round(snapshot.account.total_pnl - realized, 2) if realized is not None else None
        ),
    )


def counterfactual_for(
    snapshot: DeskSnapshot,
    action: Action,
    attribution: PnlAttribution | None,
) -> CounterfactualResult | None:
    position = snapshot.position
    if position is None or attribution is None:
        return None
    terminal_spot = snapshot.spot
    if position.strategy == "bull_put_credit":
        short_intrinsic = max(position.short_leg.strike - terminal_spot, 0)
        long_intrinsic = max(position.long_leg.strike - terminal_spot, 0)
    else:
        short_intrinsic = max(terminal_spot - position.short_leg.strike, 0)
        long_intrinsic = max(terminal_spot - position.long_leg.strike, 0)
    baseline = round(
        (position.entry_credit - short_intrinsic + long_intrinsic)
        * 100
        * position.quantity,
        2,
    )
    managed = (
        attribution.old_spread_realized_pnl
        if attribution.old_spread_realized_pnl is not None
        else baseline
    )
    assignment_exposed = (
        position.short_leg.strike * 100 * position.quantity
        if (
            position.strategy == "bull_put_credit"
            and terminal_spot < position.short_leg.strike
        )
        or (
            position.strategy == "bear_call_credit"
            and terminal_spot > position.short_leg.strike
        )
        else 0
    )
    return CounterfactualResult(
        status="modeled",
        baseline="Unmanaged spread held to the displayed terminal spot",
        baseline_pnl=baseline,
        managed_pnl=managed,
        airlock_value=round(managed - baseline, 2),
        assignment_notional_avoided=(
            assignment_exposed
            if action in {Action.CLOSE, Action.ROLL}
            and attribution.old_spread_realized_pnl is not None
            else 0
        ),
        buying_power_released=(
            round(
                (
                    abs(position.short_leg.strike - position.long_leg.strike)
                    - position.entry_credit
                )
                * 100
                * position.quantity,
                2,
            )
            if action == Action.CLOSE and attribution.old_spread_realized_pnl is not None
            else 0
        ),
        limitation=(
            "Modeled at the observed spot; it does not reproduce early assignment, broker "
            "liquidation, alternate fills, or after-hours pin movement."
        ),
    )


def build_packet(
    record: RunRecord, settings: Settings, enrollment: dict | None = None
) -> EvidencePacket:
    account_id = record.snapshot.account.account_id
    account_fingerprint = hashlib.sha256(account_id.encode()).hexdigest()[:12]
    packet = EvidencePacket(
        generated_at=datetime.now(UTC),
        code_revision=settings.code_revision,
        mode=record.mode,
        account_fingerprint=account_fingerprint,
        initial_preflight_hash=(
            str(enrollment.get("preflight_hash")) if enrollment else None
        ),
        initial_preflight_at=(
            datetime.fromisoformat(str(enrollment["captured_at"]).replace("Z", "+00:00"))
            if enrollment and enrollment.get("captured_at")
            else None
        ),
        starting_equity=record.snapshot.account.starting_equity,
        starting_equity_verified=record.snapshot.account.starting_equity_verified,
        current_equity=record.snapshot.account.equity,
        account_total_pnl=record.snapshot.account.total_pnl,
        action=record.decision.action,
        client_order_id=record.execution.client_order_id,
        broker_order_id=record.execution.order_id,
        broker_status=record.execution.broker_status,
        filled_qty=record.execution.filled_qty,
        filled_avg_price=record.execution.filled_avg_price,
        position_verified=record.execution.position_verified,
        attribution=record.attribution,
        counterfactual=record.counterfactual,
        run_id=record.id,
        run_hash=record.record_hash,
        payload_hash="pending",
        disclosure=DISCLOSURE,
    )
    packet.payload_hash = _packet_hash(packet)
    return packet


def write_packet(
    record: RunRecord, settings: Settings, enrollment: dict | None = None
) -> EvidencePacket:
    packet = build_packet(record, settings, enrollment)
    settings.evidence_path.parent.mkdir(parents=True, exist_ok=True)
    packets_dir = settings.evidence_path.parent / "evidence"
    packets_dir.mkdir(parents=True, exist_ok=True)
    immutable_path = packets_dir / f"{record.id}.json"
    immutable_temporary = packets_dir / f".{record.id}.{os.getpid()}.tmp"
    immutable_temporary.write_text(packet.model_dump_json(indent=2), encoding="utf-8")
    if immutable_path.exists():
        immutable_temporary.unlink(missing_ok=True)
    else:
        immutable_temporary.rename(immutable_path)
    temporary = settings.evidence_path.with_name(
        f".{settings.evidence_path.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(packet.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(settings.evidence_path)
    return packet


def read_packet(path: Path) -> EvidencePacket | None:
    if not path.exists():
        return None
    packet = EvidencePacket.model_validate_json(path.read_text(encoding="utf-8"))
    if packet.payload_hash != _packet_hash(packet):
        raise ValueError("Evidence packet hash does not match its contents")
    return packet


def _packet_hash(packet: EvidencePacket) -> str:
    payload = packet.model_dump(mode="json", exclude={"payload_hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
