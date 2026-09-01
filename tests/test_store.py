from datetime import UTC, date, datetime

from lastsafe.config import Settings
from lastsafe.engine import evaluate
from lastsafe.llm import DecisionService
from lastsafe.models import ExecutionReceipt, RunRecord, ScenarioRequest
from lastsafe.replay import load_replay_snapshot
from lastsafe.store import RunStore


def _record(store: RunStore, record_id: str, previous_hash: str) -> RunRecord:
    snapshot = load_replay_snapshot()
    evaluation = evaluate(
        snapshot,
        ScenarioRequest(as_of_date=date(2026, 9, 4)),
    )
    decision = DecisionService(Settings())._fallback(evaluation)
    record = RunRecord(
        id=record_id,
        created_at=datetime.now(UTC),
        mode="replay",
        snapshot=snapshot,
        evaluation=evaluation,
        decision=decision,
        execution=ExecutionReceipt(
            status="not-requested",
            action=decision.action,
            detail="test",
        ),
        previous_hash=previous_hash,
        record_hash="pending",
    )
    record.record_hash = store.hash_record(record)
    return record


def test_store_builds_and_verifies_hash_chain(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    first = store.append(_record(store, "run-one", "GENESIS"))
    second = store.append(_record(store, "run-two", first.record_hash))

    assert store.latest() == second
    assert store.verify() == (True, 2)
    store.close()
