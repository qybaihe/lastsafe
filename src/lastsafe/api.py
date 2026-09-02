from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .alpaca import AlpacaError
from .config import Settings
from .evidence import read_packet
from .models import (
    BootstrapResponse,
    EvidencePacket,
    RunRecord,
    RunRequest,
    ScenarioRequest,
    WorkerHeartbeat,
)
from .service import LastSafeService
from .store import RunStore

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_runtime()
    store = RunStore(settings.database_path)
    service = LastSafeService(settings, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        store.close()

    app = FastAPI(
        title="LastSafe",
        description="Autonomous options expiry operations on Alpaca paper trading",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.service = service
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def get_service(request: Request) -> LastSafeService:
        return request.app.state.service

    def authorize_execution(
        request: Request, x_lastsafe_execution_token: str | None = Header(default=None)
    ) -> None:
        active_settings: Settings = request.app.state.settings
        if not active_settings.execution_enabled:
            return
        if x_lastsafe_execution_token != active_settings.execution_token:
            raise HTTPException(status_code=403, detail="Invalid execution token")

    def authorize_operator(
        request: Request, x_lastsafe_execution_token: str | None
    ) -> None:
        active_settings: Settings = request.app.state.settings
        if not active_settings.execution_token:
            raise HTTPException(status_code=403, detail="Operator token is not configured")
        if x_lastsafe_execution_token != active_settings.execution_token:
            raise HTTPException(status_code=403, detail="Invalid execution token")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/styles.css", include_in_schema=False)
    async def styles() -> FileResponse:
        return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")

    @app.get("/app.js", include_in_schema=False)
    async def script() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")

    @app.get("/replay.json", include_in_schema=False)
    async def replay_fixture() -> FileResponse:
        return FileResponse(STATIC_DIR / "replay.json", media_type="application/json")

    @app.get("/health")
    async def health() -> dict[str, str | dict | None]:
        heartbeat = store.get_metadata("worker_heartbeat")
        status = "ok"
        if isinstance(heartbeat, dict):
            updated = datetime.fromisoformat(str(heartbeat["updated_at"]).replace("Z", "+00:00"))
            stale = (datetime.now(UTC) - updated).total_seconds() > (
                settings.worker_interval_seconds * 2 + 60
            )
            if heartbeat.get("status") in {"degraded", "error"} or stale:
                status = "degraded"
        elif settings.mode == "alpaca":
            status = "degraded"
        chain_valid, _ = store.verify()
        if not chain_valid or store.list_pending_intents():
            status = "degraded"
        return {
            "status": status,
            "mode": settings.mode,
            "paper": "locked",
            "worker": heartbeat,
        }

    @app.get("/api/worker", response_model=WorkerHeartbeat)
    async def worker_status() -> WorkerHeartbeat:
        heartbeat = store.get_metadata("worker_heartbeat")
        if not isinstance(heartbeat, dict):
            raise HTTPException(status_code=404, detail="Worker has not recorded a heartbeat")
        return WorkerHeartbeat.model_validate(heartbeat)

    @app.get("/api/evidence", response_model=EvidencePacket)
    async def evidence() -> EvidencePacket:
        packet = read_packet(settings.evidence_path)
        if packet is None:
            raise HTTPException(status_code=404, detail="No evidence packet has been generated")
        return packet

    @app.post("/api/competition/enroll")
    async def enroll_competition_account(
        request: Request,
        x_lastsafe_execution_token: str | None = Header(default=None),
    ) -> dict:
        authorize_operator(request, x_lastsafe_execution_token)
        try:
            return await service.enroll_competition_account()
        except AlpacaError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/bootstrap", response_model=BootstrapResponse)
    async def bootstrap(
        request: Request,
        spot_shift_pct: float = Query(0, ge=-8, le=8),
        buying_power_pct: int = Query(100, ge=0, le=100),
        minutes_to_close: int = Query(95, ge=5, le=390),
    ) -> BootstrapResponse:
        active_service = get_service(request)
        try:
            return await active_service.bootstrap(
                ScenarioRequest(
                    spot_shift_pct=spot_shift_pct,
                    buying_power_pct=buying_power_pct,
                    minutes_to_close=minutes_to_close,
                )
            )
        except AlpacaError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/runs", response_model=RunRecord)
    async def create_run(
        request: Request,
        payload: RunRequest,
        x_lastsafe_execution_token: str | None = Header(default=None),
    ) -> RunRecord:
        authorize_execution(request, x_lastsafe_execution_token)
        active_service = get_service(request)
        try:
            return await active_service.run(payload)
        except AlpacaError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/runs", response_model=list[RunRecord])
    async def list_runs(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
    ) -> list[RunRecord]:
        active_service = get_service(request)
        return active_service.store.list(limit)

    @app.get("/api/runs/latest", response_model=RunRecord)
    async def latest_run(request: Request) -> RunRecord:
        active_service = get_service(request)
        record = active_service.store.latest()
        if record is None:
            raise HTTPException(status_code=404, detail="No agent run has been recorded")
        return record

    return app
