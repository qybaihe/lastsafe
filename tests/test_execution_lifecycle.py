from pathlib import Path

import pytest

from lastsafe.alpaca import AlpacaClient
from lastsafe.config import Settings
from lastsafe.models import Action, AgentDecision, RunRequest, ScenarioRequest
from lastsafe.replay import load_replay_snapshot
from lastsafe.service import LastSafeService
from lastsafe.store import RunStore


class FakeExecutionAlpaca(AlpacaClient):
    def __init__(self, settings: Settings, statuses: list[dict], positions: list[dict]):
        super().__init__(settings)
        self.statuses = list(statuses)
        self.position_rows = positions
        self.submitted_ids: list[str] = []

    async def _query_cli_order(self, client_order_id: str):
        if self.statuses:
            row = self.statuses.pop(0)
            return {**row, "client_order_id": client_order_id}
        return None

    async def _run_cli(self, args, *, allow_not_found, allow_empty=False):
        if "submit" in args:
            client_order_id = args[args.index("--client-order-id") + 1]
            self.submitted_ids.append(client_order_id)
            return {
                "id": "submitted-order",
                "client_order_id": client_order_id,
                "status": "new",
                "filled_qty": "0",
            }
        return {}

    async def positions_via_cli(self):
        return self.position_rows


class ImmediateFillAlpaca(FakeExecutionAlpaca):
    async def _query_cli_order(self, client_order_id: str):
        return None

    async def _run_cli(self, args, *, allow_not_found, allow_empty=False):
        if "submit" in args:
            client_order_id = args[args.index("--client-order-id") + 1]
            return {
                "id": "filled-order",
                "client_order_id": client_order_id,
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "-0.25",
            }
        return {}


@pytest.mark.asyncio
async def test_terminal_polling_records_fill_lifecycle(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "lifecycle.db",
        evidence_path=tmp_path / "evidence.json",
        order_poll_seconds=0.001,
        order_timeout_seconds=1,
    )
    snapshot = load_replay_snapshot()
    roll = snapshot.roll_candidate
    assert roll is not None
    fake = FakeExecutionAlpaca(
        settings,
        statuses=[
            {"id": "order", "status": "new", "filled_qty": "0"},
            {
                "id": "order",
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "-0.25",
            },
        ],
        positions=[
            {"symbol": roll.long_leg.symbol, "qty": "1"},
            {"symbol": roll.short_leg.symbol, "qty": "-1"},
        ],
    )

    command, payload, _, recovered, lifecycle = await fake.execute_via_cli(
        "ROLL", snapshot, fake.client_order_id("ROLL", snapshot)
    )

    assert recovered is True
    assert payload["status"] == "filled"
    assert command.startswith("alpaca order submit")
    assert [event["status"] for event in lifecycle] == ["new", "filled"]


@pytest.mark.asyncio
async def test_rejected_order_is_recovered_for_service_numbered_retry(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "retry.db",
        evidence_path=tmp_path / "evidence.json",
        order_poll_seconds=0.001,
        order_timeout_seconds=1,
    )
    snapshot = load_replay_snapshot()
    fake = FakeExecutionAlpaca(
        settings,
        statuses=[{"id": "old", "status": "rejected", "filled_qty": "0"}],
        positions=[],
    )
    first = fake.client_order_id("CLOSE", snapshot)

    _, payload, _, recovered, _ = await fake.execute_via_cli("CLOSE", snapshot, first)

    assert recovered is True
    assert payload["status"] == "rejected"
    assert fake.submitted_ids == []


@pytest.mark.asyncio
async def test_position_verification_accepts_atomic_roll_book(tmp_path: Path) -> None:
    settings = Settings(
        mode="alpaca",
        database_path=tmp_path / "verified.db",
        evidence_path=tmp_path / "evidence.json",
        order_poll_seconds=0.001,
        order_timeout_seconds=1,
    )
    snapshot = load_replay_snapshot()
    roll = snapshot.roll_candidate
    assert roll is not None
    fake = ImmediateFillAlpaca(
        settings,
        statuses=[],
        positions=[
            {"symbol": roll.long_leg.symbol, "qty": "1"},
            {"symbol": roll.short_leg.symbol, "qty": "-1"},
        ],
    )
    store = RunStore(settings.database_path)
    service = LastSafeService(settings, store)
    service.alpaca = fake

    assert await service._verify_positions(Action.ROLL, snapshot) is True
    store.close()


def test_partial_terminal_fill_remains_pending_for_reconciliation(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from lastsafe.models import PendingIntent

    store = RunStore(tmp_path / "partial.db")
    intent = PendingIntent(
        incident_key="partial",
        action=Action.ROLL,
        attempt=1,
        client_order_id="lastsafe-roll-partial-a1",
        created_at=datetime.now(UTC),
        request_hash="hash",
        command_preview="alpaca order submit",
        expected_positions={},
        draft_run={},
        state="observed",
        broker_status="canceled",
    )

    store.save_intent(intent)

    assert store.list_pending_intents() == [intent]
    store.close()


@pytest.mark.asyncio
async def test_service_marks_fill_unverified_until_positions_agree(tmp_path: Path) -> None:
    settings = Settings(
        mode="alpaca",
        database_path=tmp_path / "service.db",
        evidence_path=tmp_path / "evidence.json",
        execution_enabled=True,
        execution_token="token",
        alpaca_api_key="paper-key",
        alpaca_secret_key="paper-secret",
        expected_account_id="REPLAY-7F2A",
    )
    store = RunStore(settings.database_path)
    service = LastSafeService(settings, store)
    store.set_metadata(
        "competition_account",
        {"account_id": "REPLAY-7F2A", "equity": 100_000},
    )
    snapshot = load_replay_snapshot()
    snapshot.source = "alpaca"

    class ServiceFake:
        async def snapshot(self):
            return snapshot.model_copy(deep=True)

        def client_order_id(self, action, current, attempt=1):
            return f"lastsafe-{action.lower()}-stable-a{attempt}"

        def incident_key(self, action, current):
            return f"incident-{action.lower()}"

        def command_preview(self, action, current, client_order_id):
            return "alpaca order submit"

        async def execute_via_cli(
            self,
            action,
            current,
            client_order_id,
            *,
            attempt=1,
            mutation_guard=None,
            prepare_submission=None,
        ):
            return (
                "alpaca order submit",
                {
                    "id": "filled-order",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "-0.25",
                },
                "filled",
                False,
                [{"event": "filled", "status": "filled"}],
            )

        async def positions_via_cli(self):
            return []

        @staticmethod
        def _status(payload):
            return payload["status"]

        @staticmethod
        def _terminal_statuses():
            return {"filled", "rejected", "canceled", "expired"}

    service.alpaca = ServiceFake()
    async def get_snapshot():
        current = snapshot.model_copy(deep=True)
        current.account.starting_equity_verified = True
        return current

    service.get_snapshot = get_snapshot
    async def decide(current, evaluation):
        return AgentDecision(
            action=Action.ROLL,
            source="deterministic-policy",
            model="test",
            confidence=1,
            thesis="roll",
            evidence=[],
            rejected_actions={},
        )

    service.decisions.decide = decide
    record = await service.run(
        RunRequest(
            scenario=ScenarioRequest(as_of_date=snapshot.as_of.date()),
            execute=True,
        )
    )

    assert record.execution.broker_status == "filled"
    assert record.execution.position_verified is False
    assert record.execution.status == "working"
    store.close()
