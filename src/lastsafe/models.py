from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Action(StrEnum):
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    ROLL = "ROLL"


class Quote(BaseModel):
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    timestamp: datetime
    feed: str = "indicative"


class OptionLeg(BaseModel):
    symbol: str
    position_side: Literal["long", "short"]
    option_type: Literal["call", "put"]
    strike: float = Field(gt=0)
    expiration: date
    quantity: int = Field(gt=0)
    entry_price: float = Field(ge=0)
    quote: Quote
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


class SpreadPosition(BaseModel):
    id: str
    underlying: str
    strategy: Literal["bull_put_credit", "bear_call_credit"]
    quantity: int = Field(gt=0)
    opened_at: datetime
    entry_credit: float = Field(gt=0)
    long_leg: OptionLeg
    short_leg: OptionLeg


class RollCandidate(BaseModel):
    underlying: str
    strategy: Literal["bull_put_credit", "bear_call_credit"]
    quantity: int = Field(gt=0)
    open_credit: float = Field(gt=0)
    long_leg: OptionLeg
    short_leg: OptionLeg


class AccountSnapshot(BaseModel):
    account_id: str
    equity: float = Field(gt=0)
    starting_equity: float = Field(gt=0)
    cash: float
    buying_power: float = Field(ge=0)
    options_buying_power: float = Field(ge=0)
    options_trading_level: int = Field(ge=0)
    daily_pnl: float
    total_pnl: float
    max_drawdown_pct: float = Field(le=0)
    paper: bool


class EquityPoint(BaseModel):
    timestamp: datetime
    equity: float


class DeskSnapshot(BaseModel):
    source: Literal["replay", "alpaca"]
    source_label: str
    as_of: datetime
    market_open: bool
    minutes_to_close: int = Field(ge=0)
    spot: float = Field(gt=0)
    account: AccountSnapshot
    position: SpreadPosition
    roll_candidate: RollCandidate | None
    equity_curve: list[EquityPoint]


class ScenarioRequest(BaseModel):
    spot_shift_pct: float = Field(default=0, ge=-8, le=8)
    buying_power_pct: int = Field(default=100, ge=0, le=100)
    minutes_to_close: int = Field(default=95, ge=5, le=390)
    as_of_date: date | None = None


class GateResult(BaseModel):
    key: str
    label: str
    passed: bool
    detail: str


class TerminalState(BaseModel):
    label: str
    spot: float
    pnl: float
    description: str


class ActionOutcome(BaseModel):
    action: Action
    allowed: bool
    risk_score: int = Field(ge=0, le=100)
    immediate_cashflow: float
    locked_or_max_pnl: float
    assignment_notional: float = Field(ge=0)
    headline: str
    detail: str
    blockers: list[str] = Field(default_factory=list)
    terminal_states: list[TerminalState] = Field(default_factory=list)


class Evaluation(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    evaluated_at: datetime
    scenario: ScenarioRequest
    effective_spot: float
    effective_buying_power: float
    dte: int = Field(ge=0)
    short_distance_pct: float
    close_debit: float
    roll_open_credit: float | None
    roll_net_credit: float | None
    max_loss: float
    gates: list[GateResult]
    outcomes: list[ActionOutcome]
    policy_action: Action
    urgency: Literal["nominal", "watch", "critical"]


class AgentDecision(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    action: Action
    source: Literal["llm", "deterministic-policy"]
    model: str
    confidence: float = Field(ge=0, le=1)
    thesis: str
    evidence: list[str]
    rejected_actions: dict[str, str]
    policy_override: bool = False


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    status: Literal[
        "not-requested", "simulated", "submitted", "recovered", "blocked", "failed"
    ]
    action: Action
    client_order_id: str | None = None
    order_id: str | None = None
    command_preview: str | None = None
    submitted_at: datetime | None = None
    broker_status: str | None = None
    detail: str
    raw: dict = Field(default_factory=dict)


class RunRequest(BaseModel):
    scenario: ScenarioRequest = Field(default_factory=ScenarioRequest)
    execute: bool = False


class RunRecord(BaseModel):
    id: str
    created_at: datetime
    mode: Literal["replay", "alpaca"]
    snapshot: DeskSnapshot
    evaluation: Evaluation
    decision: AgentDecision
    execution: ExecutionReceipt
    previous_hash: str
    record_hash: str


class BootstrapResponse(BaseModel):
    snapshot: DeskSnapshot
    evaluation: Evaluation
    latest_run: RunRecord | None
    capabilities: dict[str, bool | str]
