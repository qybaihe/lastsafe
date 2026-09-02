from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    mode: Literal["replay", "alpaca"] = "replay"
    database_path: Path = Path("data/lastsafe.db")
    execution_enabled: bool = False
    execution_token: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_data_feed: str = "indicative"
    alpaca_cli_path: str = "alpaca"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.featherless.ai/v1"
    llm_model: str = "zai-org/GLM-5.2"
    expected_account_id: str = ""
    worker_interval_seconds: int = 300
    order_timeout_seconds: int = 90
    order_poll_seconds: float = 2.0
    evidence_path: Path = Path("data/competition-evidence.json")
    code_revision: str = "development"

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.getenv("LASTSAFE_MODE", "replay").strip().lower()
        if mode not in {"replay", "alpaca"}:
            raise ValueError("LASTSAFE_MODE must be 'replay' or 'alpaca'")
        return cls(
            mode=cast(Literal["replay", "alpaca"], mode),
            database_path=Path(os.getenv("LASTSAFE_DATABASE_PATH", "data/lastsafe.db")),
            execution_enabled=_is_true(os.getenv("LASTSAFE_EXECUTION_ENABLED")),
            execution_token=os.getenv("LASTSAFE_EXECUTION_TOKEN", ""),
            alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
            alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
            alpaca_data_feed=os.getenv("ALPACA_DATA_FEED", "indicative"),
            alpaca_cli_path=os.getenv("ALPACA_CLI_PATH", "alpaca"),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.featherless.ai/v1"),
            llm_model=os.getenv("LLM_MODEL", "zai-org/GLM-5.2"),
            expected_account_id=os.getenv("LASTSAFE_EXPECTED_ACCOUNT_ID", ""),
            worker_interval_seconds=int(os.getenv("LASTSAFE_WORKER_INTERVAL_SECONDS", "300")),
            order_timeout_seconds=int(os.getenv("LASTSAFE_ORDER_TIMEOUT_SECONDS", "90")),
            order_poll_seconds=float(os.getenv("LASTSAFE_ORDER_POLL_SECONDS", "2")),
            evidence_path=Path(
                os.getenv("LASTSAFE_EVIDENCE_PATH", "data/competition-evidence.json")
            ),
            code_revision=os.getenv("LASTSAFE_CODE_REVISION", "development"),
        )

    def validate_runtime(self) -> None:
        if self.mode == "alpaca" and not (self.alpaca_api_key and self.alpaca_secret_key):
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required in alpaca mode")
        if self.execution_enabled and not self.execution_token:
            raise ValueError("LASTSAFE_EXECUTION_TOKEN is required when execution is enabled")
        if self.mode == "alpaca" and self.execution_enabled and not self.expected_account_id:
            raise ValueError(
                "LASTSAFE_EXPECTED_ACCOUNT_ID is required when Alpaca execution is enabled"
            )
        if not 15 <= self.worker_interval_seconds <= 3600:
            raise ValueError("worker interval must be between 15 and 3600 seconds")
