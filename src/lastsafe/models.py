from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Action(StrEnum):
    OPEN = "OPEN"
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    ROLL = "ROLL"
    STAND_DOWN = "STAND_DOWN"


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

    @model_validator(mode="after")
    def validate_vertical(self) -> SpreadPosition:
        _validate_vertical_legs(self.long_leg, self.short_leg, self.quantity, self.strategy)
        width = abs(self.short_leg.strike - self.long_leg.strike)
        if self.entry_credit >= width:
            raise ValueError("entry credit must be smaller than spread width")
        return self


class RollCandidate(BaseModel):
    underlying: str
    strategy: Literal["bull_put_credit", "bear_call_credit"]
    quantity: int = Field(gt=0)
    open_credit: float = Field(gt=0)
    long_leg: OptionLeg
    short_leg: OptionLeg

    @model_validator(mode="after")
    def validate_vertical(self) -> RollCandidate:
        _validate_vertical_legs(self.long_leg, self.short_leg, self.quantity, self.strategy)
        width = abs(self.short_leg.strike - self.long_leg.strike)
        if self.open_credit >= width:
            raise ValueError("roll credit must be smaller than spread width")
        return self


class EntryCandidate(BaseModel):
    id: str
    underlying: str
    strategy: Literal["bull_put_credit", "bear_call_credit"]
    quantity: int = Field(default=1, gt=0)
    open_credit: float = Field(gt=0)
    max_loss: float = Field(gt=0)
    dte: int = Field(ge=1)
    short_clearance_pct: float = Field(gt=0)
    trend_5d_pct: float
    price_vs_sma20_pct: float
    rationale: str
    long_leg: OptionLeg
    short_leg: OptionLeg

    @model_validator(mode="after")
    def validate_vertical(self) -> EntryCandidate:
        _validate_vertical_legs(self.long_leg, self.short_leg, self.quantity, self.strategy)
        width = abs(self.short_leg.strike - self.long_leg.strike)
        if self.open_credit >= width:
            raise ValueError("entry credit must be smaller than spread width")
        expected_loss = (width - self.open_credit) * 100 * self.quantity
        if abs(self.max_loss - expected_loss) > 0.011:
            raise ValueError("entry maximum loss does not match width and credit")
        return self


def _validate_vertical_legs(
    long_leg: OptionLeg,
    short_leg: OptionLeg,
    quantity: int,
    strategy: str,
) -> None:
    if long_leg.position_side != "long" or short_leg.position_side != "short":
        raise ValueError("vertical must contain one long and one short leg")
    if long_leg.quantity != quantity or short_leg.quantity != quantity:
        raise ValueError("vertical leg quantities must exactly match parent quantity")
    if long_leg.option_type != short_leg.option_type:
        raise ValueError("vertical legs must use the same option type")
    if long_leg.expiration != short_leg.expiration:
        raise ValueError("vertical legs must use the same expiration")
    long_symbol = _symbol_root(long_leg.symbol)
    short_symbol = _symbol_root(short_leg.symbol)
    if not long_symbol or long_symbol != short_symbol:
        raise ValueError("vertical legs must use the same underlying")
    protected = (
        long_leg.strike < short_leg.strike
        if strategy == "bull_put_credit"
        else long_leg.strike > short_leg.strike
    )
    if not protected:
        raise ValueError("long leg does not protect the short leg")


def _symbol_root(symbol: str) -> str:
    for index, character in enumerate(symbol):
        if character.isdigit():
            return symbol[:index]
    return ""


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
    status: str = "ACTIVE"
    trading_blocked: bool = False
    starting_equity_verified: bool = False


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
    position: SpreadPosition | None = None
    roll_candidate: RollCandidate | None = None
    entry_candidate: EntryCandidate | None = None
    open_order_count: int = Field(default=0, ge=0)
    portfolio_issues: list[str] = Field(default_factory=list)
    equity_curve: list[EquityPoint]

    def lifecycle_state(self) -> Literal["flat", "positioned", "unsupported"]:
        if self.portfolio_issues:
            return "unsupported"
        return "positioned" if self.position else "flat"


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
    lifecycle_state: Literal["flat", "positioned", "unsupported"] = "positioned"
    scenario: ScenarioRequest
    effective_spot: float
    effective_buying_power: float
    dte: int = Field(ge=0)
    short_distance_pct: float | None = None
    close_debit: float | None = None
    roll_open_credit: float | None = None
    roll_net_credit: float | None = None
    max_loss: float = Field(ge=0)
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
        "not-requested",
        "simulated",
        "submitted",
        "working",
        "filled",
        "recovered",
        "canceled",
        "rejected",
        "expired",
        "blocked",
        "failed",
        "unknown",
    ]
    action: Action
    client_order_id: str | None = None
    order_id: str | None = None
    command_preview: str | None = None
    submitted_at: datetime | None = None
    broker_status: str | None = None
    detail: str
    raw: dict = Field(default_factory=dict)
    attempt: int = Field(default=1, ge=1)
    terminal_at: datetime | None = None
    filled_qty: float | None = None
    filled_avg_price: float | None = None
    position_verified: bool = False
    lifecycle: list[dict] = Field(default_factory=list)


class PendingIntent(BaseModel):
    incident_key: str
    action: Action
    attempt: int
    client_order_id: str
    created_at: datetime
    request_hash: str
    command_preview: str
    expected_positions: dict[str, float]
    draft_run: dict
    state: Literal["prepared", "submitting", "observed", "terminal"]
    broker_order_id: str | None = None
    broker_status: str | None = None


class PnlAttribution(BaseModel):
    basis_credit: float
    close_debit: float | None = None
    old_spread_realized_pnl: float | None = None
    new_spread_open_credit: float | None = None
    new_spread_unrealized_pnl: float | None = None
    account_total_pnl: float
    other_account_movement: float | None = None


class CounterfactualResult(BaseModel):
    status: Literal["modeled", "realized"]
    baseline: str
    baseline_pnl: float
    managed_pnl: float
    airlock_value: float
    assignment_notional_avoided: float = Field(ge=0)
    buying_power_released: float = Field(ge=0)
    limitation: str


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
    attribution: PnlAttribution | None = None
    counterfactual: CounterfactualResult | None = None
    trigger: Literal["manual", "scheduler", "recovery"] = "manual"
    previous_hash: str
    record_hash: str


class BootstrapResponse(BaseModel):
    snapshot: DeskSnapshot
    evaluation: Evaluation
    latest_run: RunRecord | None
    capabilities: dict[str, bool | str]


class WorkerHeartbeat(BaseModel):
    status: Literal["idle", "running", "healthy", "degraded", "error", "stopped"]
    updated_at: datetime
    owner: str
    last_run_id: str | None = None
    last_action: Action | None = None
    last_error: str | None = None
    next_run_at: datetime | None = None


class EvidencePacket(BaseModel):
    schema_version: Literal["lastsafe.competition-evidence.v1"] = (
        "lastsafe.competition-evidence.v1"
    )
    generated_at: datetime
    code_revision: str
    mode: Literal["replay", "alpaca"]
    account_fingerprint: str
    initial_preflight_hash: str | None = None
    initial_preflight_at: datetime | None = None
    starting_equity: float
    starting_equity_verified: bool
    current_equity: float
    account_total_pnl: float
    action: Action
    client_order_id: str | None
    broker_order_id: str | None
    broker_status: str | None
    filled_qty: float | None
    filled_avg_price: float | None
    position_verified: bool
    attribution: PnlAttribution | None
    counterfactual: CounterfactualResult | None
    run_id: str
    run_hash: str
    payload_hash: str
    disclosure: str
