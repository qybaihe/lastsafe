import hashlib
from datetime import UTC, date, datetime

import pytest

from lastsafe.config import Settings
from lastsafe.engine import evaluate
from lastsafe.evidence import (
    attribution_for,
    build_packet,
    counterfactual_for,
    read_packet,
    write_packet,
)
from lastsafe.llm import DecisionService
from lastsafe.models import Action, ExecutionReceipt, RunRecord, ScenarioRequest
from lastsafe.replay import load_replay_snapshot


def test_evidence_packet_redacts_full_account_id_and_hashes_payload(tmp_path) -> None:
    snapshot = load_replay_snapshot()
    evaluation = evaluate(snapshot, ScenarioRequest(as_of_date=date(2026, 9, 4)))
    decision = DecisionService(Settings())._fallback(evaluation)
    execution = ExecutionReceipt(
        status="filled",
        action=Action.ROLL,
        client_order_id="lastsafe-roll-demo-a1",
        order_id="broker-order",
        broker_status="filled",
        detail="filled",
        filled_qty=1,
        filled_avg_price=-0.25,
        position_verified=True,
    )
    attribution = attribution_for(snapshot, Action.ROLL, execution)
    counterfactual = counterfactual_for(snapshot, Action.ROLL, attribution)
    record = RunRecord(
        id="evidence-run",
        created_at=datetime.now(UTC),
        mode="replay",
        snapshot=snapshot,
        evaluation=evaluation,
        decision=decision,
        execution=execution,
        attribution=attribution,
        counterfactual=counterfactual,
        previous_hash="GENESIS",
        record_hash="run-hash",
    )
    settings = Settings(evidence_path=tmp_path / "evidence.json", code_revision="abc123")

    packet = write_packet(record, settings)

    assert packet.account_fingerprint == hashlib.sha256(b"REPLAY-7F2A").hexdigest()[:12]
    assert "REPLAY-7F2A" not in settings.evidence_path.read_text()
    assert packet.payload_hash != "pending"
    assert read_packet(settings.evidence_path) == packet
    assert build_packet(record, settings).code_revision == "abc123"


def test_counterfactual_reports_assignment_notional_avoided() -> None:
    snapshot = load_replay_snapshot()
    execution = ExecutionReceipt(
        status="simulated",
        action=Action.CLOSE,
        detail="demo",
    )
    attribution = attribution_for(snapshot, Action.CLOSE, execution)
    result = counterfactual_for(snapshot, Action.CLOSE, attribution)

    assert result is not None
    assert result.assignment_notional_avoided == 64_000
    assert result.airlock_value == -100


def test_canceled_order_never_reports_realized_pnl_or_avoided_assignment() -> None:
    snapshot = load_replay_snapshot()
    execution = ExecutionReceipt(
        status="canceled",
        action=Action.CLOSE,
        broker_status="canceled",
        detail="not filled",
    )

    attribution = attribution_for(snapshot, Action.CLOSE, execution)
    counterfactual = counterfactual_for(snapshot, Action.CLOSE, attribution)

    assert attribution is not None
    assert attribution.old_spread_realized_pnl is None
    assert counterfactual is not None
    assert counterfactual.assignment_notional_avoided == 0
    assert counterfactual.buying_power_released == 0


def test_evidence_reader_rejects_tampering(tmp_path) -> None:
    snapshot = load_replay_snapshot()
    evaluation = evaluate(snapshot, ScenarioRequest(as_of_date=date(2026, 9, 4)))
    decision = DecisionService(Settings())._fallback(evaluation)
    record = RunRecord(
        id="tamper-run",
        created_at=datetime.now(UTC),
        mode="replay",
        snapshot=snapshot,
        evaluation=evaluation,
        decision=decision,
        execution=ExecutionReceipt(
            status="not-requested", action=decision.action, detail="test"
        ),
        previous_hash="GENESIS",
        record_hash="run-hash",
    )
    settings = Settings(evidence_path=tmp_path / "latest.json")
    write_packet(record, settings)
    payload = settings.evidence_path.read_text().replace("100482.3", "999999.0")
    settings.evidence_path.write_text(payload)

    with pytest.raises(ValueError, match="hash"):
        read_packet(settings.evidence_path)
