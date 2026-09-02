from datetime import UTC, date, datetime

from lastsafe.config import Settings
from lastsafe.engine import evaluate
from lastsafe.llm import DecisionService
from lastsafe.models import Action, ExecutionReceipt, PendingIntent, RunRecord, ScenarioRequest
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


def test_store_metadata_round_trip(tmp_path) -> None:
    store = RunStore(tmp_path / "metadata.db")

    store.set_metadata("competition", {"starting_equity": 100_000})

    assert store.get_metadata("competition") == {"starting_equity": 100_000}
    store.close()


def test_store_persists_numbered_order_attempts(tmp_path) -> None:
    store = RunStore(tmp_path / "attempts.db")

    assert store.current_attempt("incident") == 1
    assert store.advance_attempt("incident") == 2
    assert store.current_attempt("incident") == 2
    store.close()


def test_store_persists_pending_intent_before_broker_side_effect(tmp_path) -> None:
    store = RunStore(tmp_path / "intent.db")
    intent = PendingIntent(
        incident_key="incident",
        action=Action.CLOSE,
        attempt=1,
        client_order_id="lastsafe-close-a1",
        created_at=datetime.now(UTC),
        request_hash="hash",
        command_preview="alpaca order submit",
        expected_positions={},
        draft_run={},
        state="submitting",
    )

    store.save_intent(intent)

    assert store.get_intent("incident") == intent
    assert store.list_pending_intents() == [intent]
    intent.state = "terminal"
    store.save_intent(intent)
    assert store.list_pending_intents() == []
    store.close()


def test_lease_is_atomic_across_store_connections(tmp_path) -> None:
    path = tmp_path / "shared.db"
    first = RunStore(path)
    second = RunStore(path)

    token = first.acquire_lease("one", ttl_seconds=60)
    assert token is not None
    assert second.acquire_lease("two", ttl_seconds=60) is None
    assert first.renew_lease(token, ttl_seconds=60) is True
    assert first.release_lease(token) is True

    first.close()
    second.close()


def test_stale_lease_token_cannot_release_new_owner(tmp_path, monkeypatch) -> None:
    store = RunStore(tmp_path / "fenced.db")
    times = iter([100.0, 102.0])
    monkeypatch.setattr("lastsafe.store.time.time", lambda: next(times))

    old = store.acquire_lease("old", ttl_seconds=1)
    new = store.acquire_lease("new", ttl_seconds=60)

    assert old is not None and new is not None
    assert store.release_lease(old) is False
    assert store.release_lease(new) is True
    store.close()


def test_unknown_hash_schema_fails_closed(tmp_path) -> None:
    store = RunStore(tmp_path / "schema.db")
    first = store.append(_record(store, "run-one", "GENESIS"))
    assert first.record_hash
    store.set_metadata("hash_schema_version", "1")

    assert store.verify() == (False, 1)
    store.close()
