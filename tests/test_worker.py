from pathlib import Path

import pytest

from lastsafe.config import Settings
from lastsafe.service import LastSafeService
from lastsafe.store import RunStore
from lastsafe.worker import LastSafeWorker


@pytest.mark.asyncio
async def test_worker_records_heartbeat_and_scheduler_run(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "worker.db",
        evidence_path=tmp_path / "evidence.json",
    )
    store = RunStore(settings.database_path)
    worker = LastSafeWorker(settings, LastSafeService(settings, store), store)

    heartbeat = await worker.cycle()
    latest = store.latest()

    assert heartbeat.status == "healthy"
    assert latest is not None
    assert latest.trigger == "scheduler"
    assert store.get_metadata("worker_heartbeat")["last_run_id"] == latest.id
    store.close()


def test_worker_lease_is_single_flight(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "lease.db")

    token = store.acquire_lease("one", ttl_seconds=60)
    assert token is not None
    assert store.acquire_lease("two", ttl_seconds=60) is None
    assert store.release_lease(token) is True
    assert store.acquire_lease("two", ttl_seconds=60) is not None
    store.close()
