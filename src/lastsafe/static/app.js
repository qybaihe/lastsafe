const state = {
  snapshot: null,
  evaluation: null,
  latestRun: null,
  runs: [],
  capabilities: {},
  debounce: null,
};

const staticMode =
  window.location.hostname.endsWith("github.io") ||
  new URLSearchParams(window.location.search).has("static");
const staticLedgerKey = "lastsafe-replay-ledger-v1";
let staticSnapshot = null;

const $ = (selector) => document.querySelector(selector);
const money = (value, digits = 0) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
const signedMoney = (value) => `${value >= 0 ? "+" : ""}${money(value)}`;
const signedPct = (value, digits = 2) =>
  `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
const shortDate = (value) =>
  new Date(`${value}T12:00:00Z`)
    .toLocaleDateString("en-US", { month: "short", day: "2-digit" })
    .toUpperCase();
const safe = (value) =>
  String(value ?? "").replace(
    /[&<>'"]/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "'": "&#39;",
        '"': "&quot;",
      })[char],
  );

function scenario() {
  return {
    spot_shift_pct: Number($("#spotShift").value),
    buying_power_pct: Number($("#buyingPower").value),
    minutes_to_close: Number($("#minutesClose").value),
    as_of_date: state.snapshot?.as_of?.slice(0, 10) ?? null,
  };
}

async function request(url, options = {}) {
  if (staticMode) return staticRequest(url, options);
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return response.json();
}

async function staticRequest(url, options = {}) {
  if (!staticSnapshot) {
    const response = await fetch("./replay.json");
    if (!response.ok) throw new Error("Replay fixture could not be loaded");
    staticSnapshot = await response.json();
  }
  const runs = JSON.parse(localStorage.getItem(staticLedgerKey) || "[]");
  if (url.startsWith("/api/bootstrap")) {
    const currentScenario = scenario();
    return {
      snapshot: structuredClone(staticSnapshot),
      evaluation: evaluateStatic(staticSnapshot, currentScenario),
      latest_run: runs[0] || null,
      capabilities: {
        mode: "replay",
        paper_only: true,
        execution_enabled: false,
        llm_configured: false,
        alpaca_configured: false,
        audit_chain_valid: true,
        audit_chain_length: String(runs.length),
      },
    };
  }
  if (url.startsWith("/api/runs") && options.method === "POST") {
    const payload = JSON.parse(options.body);
    const evaluation = evaluateStatic(staticSnapshot, payload.scenario);
    const record = await createStaticRun(
      staticSnapshot,
      evaluation,
      payload.execute,
      runs,
    );
    const nextRuns = [record, ...runs].slice(0, 50);
    localStorage.setItem(staticLedgerKey, JSON.stringify(nextRuns));
    return record;
  }
  if (url.startsWith("/api/runs")) return runs.slice(0, 8);
  throw new Error(`Unsupported static route: ${url}`);
}

function evaluateStatic(snapshot, input) {
  const scenarioValue = {
    spot_shift_pct: Number(input.spot_shift_pct || 0),
    buying_power_pct: Number(input.buying_power_pct ?? 100),
    minutes_to_close: Number(input.minutes_to_close ?? 95),
    as_of_date: input.as_of_date || snapshot.as_of.slice(0, 10),
  };
  const position = snapshot.position;
  const roll = snapshot.roll_candidate;
  const spot = round(snapshot.spot * (1 + scenarioValue.spot_shift_pct / 100));
  const buyingPower = round(
    (snapshot.account.options_buying_power * scenarioValue.buying_power_pct) /
      100,
  );
  const width = Math.abs(position.short_leg.strike - position.long_leg.strike);
  const maxLoss = round(
    (width - position.entry_credit) * 100 * position.quantity,
  );
  const closeDebit = round(
    Math.max(position.short_leg.quote.ask - position.long_leg.quote.bid, 0),
  );
  const distance = round(
    position.strategy === "bull_put_credit"
      ? ((spot - position.short_leg.strike) / spot) * 100
      : ((position.short_leg.strike - spot) / spot) * 100,
    3,
  );
  const dte = Math.max(
    0,
    Math.round(
      (Date.parse(`${position.short_leg.expiration}T12:00:00Z`) -
        Date.parse(`${scenarioValue.as_of_date}T12:00:00Z`)) /
        86400000,
    ),
  );
  const assignmentNotional =
    position.short_leg.strike * 100 * position.quantity;
  const rollNetCredit = roll ? round(roll.open_credit - closeDebit) : null;
  const rollWidth = roll
    ? Math.abs(roll.short_leg.strike - roll.long_leg.strike)
    : 0;
  const rollMaxLoss = roll
    ? round((rollWidth - roll.open_credit) * 100 * roll.quantity)
    : null;
  const rollDistance = roll
    ? round(
        roll.strategy === "bull_put_credit"
          ? ((spot - roll.short_leg.strike) / spot) * 100
          : ((roll.short_leg.strike - spot) / spot) * 100,
        3,
      )
    : -100;
  const accountReady =
    snapshot.account.paper &&
    snapshot.account.options_trading_level >= 3 &&
    Math.abs(snapshot.account.starting_equity - 100000) < 0.01;
  const riskReady = maxLoss <= snapshot.account.equity * 0.01;
  const buyingPowerReady = buyingPower >= maxLoss;
  const shortItm = distance < 0;
  const pinZone = distance < 1.25;
  const expiryPressure = dte === 0 && scenarioValue.minutes_to_close <= 120;
  const holdAllowed =
    accountReady &&
    !(expiryPressure || shortItm || buyingPower < assignmentNotional * 0.25);
  const closeAllowed = accountReady && snapshot.market_open && riskReady;
  const rollAllowed = Boolean(
    roll &&
      closeAllowed &&
      buyingPowerReady &&
      rollNetCredit >= 0.05 &&
      rollMaxLoss <= snapshot.account.equity * 0.01 &&
      rollDistance >= 0.75,
  );
  const terminalSpots = [
    ["risk-off", position.long_leg.strike - width],
    ["pin-zone", position.short_leg.strike],
    ["clear", position.short_leg.strike + width],
  ];
  const terminalStates = terminalSpots.map(([label, terminalSpot]) => ({
    label,
    spot: terminalSpot,
    pnl: terminalPayoff(position, terminalSpot),
    description:
      label === "risk-off"
        ? "Both legs finish in the money"
        : label === "pin-zone"
          ? "Short strike is exposed to pin risk"
          : "Spread expires out of the money",
  }));
  const holdBlockers = [];
  if (expiryPressure) holdBlockers.push("Inside the 120-minute expiry airlock");
  if (shortItm) holdBlockers.push("Short option is in the money");
  if (buyingPower < assignmentNotional * 0.25)
    holdBlockers.push("Post-expiry share obligation exceeds reserve policy");
  const rollBlockers = [];
  if (!roll)
    rollBlockers.push("No later-expiry vertical passed contract selection");
  if (roll && rollDistance < 0.75)
    rollBlockers.push("New short strike leaves less than 0.75% spot clearance");
  if (!buyingPowerReady)
    rollBlockers.push("Insufficient options buying-power reserve");
  const closePnl = round(
    (position.entry_credit - closeDebit) * 100 * position.quantity,
  );
  const outcomes = [
    {
      action: "HOLD",
      allowed: holdAllowed,
      risk_score: Math.min(
        100,
        Math.floor(
          35 + Math.max(0, 1.5 - distance) * 24 + (expiryPressure ? 45 : 0),
        ),
      ),
      immediate_cashflow: 0,
      locked_or_max_pnl: round(position.entry_credit * 100 * position.quantity),
      assignment_notional: assignmentNotional,
      headline: "Keep the current spread into expiry",
      detail: `At ${position.underlying} $${spot.toFixed(2)}, the short strike has ${distance >= 0 ? "+" : ""}${distance.toFixed(2)}% clearance. Pin risk remains discontinuous into the close.`,
      blockers: holdBlockers,
      terminal_states: terminalStates,
    },
    {
      action: "CLOSE",
      allowed: closeAllowed,
      risk_score: Math.max(5, Math.floor(20 + (closeDebit / width) * 15)),
      immediate_cashflow: round(-closeDebit * 100 * position.quantity),
      locked_or_max_pnl: closePnl,
      assignment_notional: 0,
      headline: "Buy back the vertical and stop the clock",
      detail: `Conservative two-sided close costs $${(closeDebit * 100).toFixed(0)}; estimated realized P&L becomes $${closePnl >= 0 ? "+" : ""}${closePnl.toFixed(0)}.`,
      blockers: closeAllowed ? [] : ["Paper account or session check failed"],
      terminal_states: [],
    },
    {
      action: "ROLL",
      allowed: rollAllowed,
      risk_score: Math.min(
        100,
        Math.max(
          8,
          Math.floor(
            28 + Math.max(0, 1.25 - rollDistance) * 20 + (rollAllowed ? 0 : 25),
          ),
        ),
      ),
      immediate_cashflow: round((rollNetCredit || 0) * 100 * position.quantity),
      locked_or_max_pnl: round(-(rollMaxLoss || maxLoss)),
      assignment_notional: roll
        ? roll.short_leg.strike * 100 * roll.quantity
        : 0,
      headline: "Atomically close and reopen one week out",
      detail: roll
        ? `Four legs move as one MLeg order for an estimated $${rollNetCredit >= 0 ? "+" : ""}${(rollNetCredit * 100).toFixed(0)} net credit and ${rollDistance.toFixed(2)}% new short-strike clearance.`
        : "No valid later-expiry vertical is currently available.",
      blockers: rollBlockers,
      terminal_states: [],
    },
  ];
  const policyAction =
    expiryPressure && rollAllowed
      ? "ROLL"
      : (expiryPressure || shortItm || !buyingPowerReady) && closeAllowed
        ? "CLOSE"
        : holdAllowed
          ? "HOLD"
          : "CLOSE";
  return {
    evaluated_at: new Date().toISOString(),
    scenario: scenarioValue,
    effective_spot: spot,
    effective_buying_power: buyingPower,
    dte,
    short_distance_pct: distance,
    close_debit: closeDebit,
    roll_open_credit: roll?.open_credit ?? null,
    roll_net_credit: rollNetCredit,
    max_loss: maxLoss,
    gates: [
      {
        key: "paper-lock",
        label: "Paper endpoint lock",
        passed: true,
        detail: "Account is paper-only",
      },
      {
        key: "options-level",
        label: "Options level 3",
        passed: true,
        detail: "Effective level 3",
      },
      {
        key: "competition-account",
        label: "$100K competition account",
        passed: true,
        detail: "Starting equity $100,000.00",
      },
      {
        key: "market-session",
        label: "Options session open",
        passed: true,
        detail: `${scenarioValue.minutes_to_close} minutes until close`,
      },
      {
        key: "quote-freshness",
        label: "Fresh two-sided quotes",
        passed: true,
        detail: "Replay quotes frozen at incident time",
      },
      {
        key: "defined-risk",
        label: "Defined-risk vertical",
        passed: riskReady,
        detail: `Maximum loss $${maxLoss.toFixed(0)} (${((maxLoss / snapshot.account.equity) * 100).toFixed(2)}% equity)`,
      },
      {
        key: "buying-power",
        label: "Buying-power reserve",
        passed: buyingPowerReady,
        detail: `$${buyingPower.toLocaleString()} available after scenario`,
      },
    ],
    outcomes,
    policy_action: policyAction,
    urgency:
      expiryPressure && (shortItm || pinZone)
        ? "critical"
        : dte <= 1 || pinZone
          ? "watch"
          : "nominal",
  };
}

async function createStaticRun(snapshot, evaluation, execute, runs) {
  const action = evaluation.policy_action;
  const selected = evaluation.outcomes.find(
    (outcome) => outcome.action === action,
  );
  const rejected = Object.fromEntries(
    evaluation.outcomes
      .filter((outcome) => outcome.action !== action)
      .map((outcome) => [
        outcome.action,
        outcome.blockers[0] ||
          `Higher risk score (${outcome.risk_score}) than ${action}`,
      ]),
  );
  const decision = {
    action,
    source: "deterministic-policy",
    model: "lastsafe-airlock-v1",
    confidence: evaluation.urgency === "critical" ? 0.94 : 0.82,
    thesis: `Select ${action}: ${selected.detail} The public replay uses the deterministic fallback and never sends a broker request.`,
    evidence: [
      `DTE ${evaluation.dte}; ${evaluation.scenario.minutes_to_close} minutes to close`,
      `Short-strike clearance ${evaluation.short_distance_pct >= 0 ? "+" : ""}${evaluation.short_distance_pct.toFixed(2)}%`,
      `Options buying power $${evaluation.effective_buying_power.toLocaleString()}`,
    ],
    rejected_actions: rejected,
    policy_override: false,
  };
  const clientOrderId = await stableClientOrderId(action, snapshot);
  const execution =
    !execute || action === "HOLD"
      ? {
          status: "not-requested",
          action,
          client_order_id: null,
          order_id: null,
          command_preview: null,
          submitted_at: null,
          broker_status: null,
          detail: execute
            ? "HOLD is an autonomous no-order action."
            : "No order requested; decision is recorded for audit.",
          raw: {},
        }
      : {
          status: "simulated",
          action,
          client_order_id: clientOrderId,
          order_id: `REPLAY-${(await sha256(clientOrderId)).slice(0, 8).toUpperCase()}`,
          command_preview: commandPreview(action, snapshot, clientOrderId),
          submitted_at: new Date().toISOString(),
          broker_status: "accepted (replay)",
          detail:
            "GitHub Pages replay simulated the exact paper CLI payload; no broker request was sent.",
          raw: {},
        };
  const record = {
    id: `run_${Date.now().toString(36)}`,
    created_at: new Date().toISOString(),
    mode: "replay",
    snapshot: structuredClone(snapshot),
    evaluation,
    decision,
    execution,
    previous_hash: runs[0]?.record_hash || "GENESIS",
    record_hash: "pending",
  };
  record.record_hash = await sha256(JSON.stringify(record));
  return record;
}

function commandPreview(action, snapshot, clientOrderId) {
  const position = snapshot.position;
  const closeDebit = round(
    position.short_leg.quote.ask - position.long_leg.quote.bid,
  );
  const legs = [
    {
      symbol: position.short_leg.symbol,
      ratio_qty: "1",
      side: "buy",
      position_intent: "buy_to_close",
    },
    {
      symbol: position.long_leg.symbol,
      ratio_qty: "1",
      side: "sell",
      position_intent: "sell_to_close",
    },
  ];
  let limitPrice = closeDebit;
  if (action === "ROLL") {
    const roll = snapshot.roll_candidate;
    limitPrice = -(roll.open_credit - closeDebit);
    legs.push(
      {
        symbol: roll.long_leg.symbol,
        ratio_qty: "1",
        side: "buy",
        position_intent: "buy_to_open",
      },
      {
        symbol: roll.short_leg.symbol,
        ratio_qty: "1",
        side: "sell",
        position_intent: "sell_to_open",
      },
    );
  }
  return `alpaca order submit --type limit --time-in-force day --order-class mleg --qty ${position.quantity} --limit-price ${limitPrice.toFixed(2)} --legs '${JSON.stringify(legs)}' --client-order-id ${clientOrderId}`;
}

async function stableClientOrderId(action, snapshot) {
  const rollSymbols =
    action === "ROLL"
      ? `-${snapshot.roll_candidate.long_leg.symbol}-${snapshot.roll_candidate.short_leg.symbol}`
      : "";
  const identity = `${snapshot.account.account_id}-${snapshot.position.id}-${action}${rollSymbols}`;
  return `lastsafe-${action.toLowerCase()}-${(await sha256(identity)).slice(0, 20)}`;
}

async function sha256(value) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function terminalPayoff(position, terminalSpot) {
  const shortIntrinsic =
    position.strategy === "bull_put_credit"
      ? Math.max(position.short_leg.strike - terminalSpot, 0)
      : Math.max(terminalSpot - position.short_leg.strike, 0);
  const longIntrinsic =
    position.strategy === "bull_put_credit"
      ? Math.max(position.long_leg.strike - terminalSpot, 0)
      : Math.max(terminalSpot - position.long_leg.strike, 0);
  return round(
    (position.entry_credit - shortIntrinsic + longIntrinsic) *
      100 *
      position.quantity,
  );
}

function round(value, digits = 2) {
  const factor = 10 ** digits;
  return Math.round((value + Number.EPSILON) * factor) / factor;
}

async function bootstrap() {
  const params = new URLSearchParams(scenario());
  const data = await request(`/api/bootstrap?${params}`);
  state.snapshot = data.snapshot;
  state.evaluation = data.evaluation;
  state.latestRun = data.latest_run;
  state.capabilities = data.capabilities;
  render();
}

function render() {
  const { snapshot, evaluation, capabilities } = state;
  if (!snapshot || !evaluation) return;

  $("#connectionText").textContent =
    snapshot.source === "alpaca"
      ? "ALPACA PAPER CONNECTED"
      : "REPLAY ENGINE ONLINE";
  $("#modeStamp").textContent =
    snapshot.source === "alpaca" ? "PAPER ACCOUNT" : "REPLAY MODE";
  $("#modeStamp").title = snapshot.source_label;
  $("#heroCountdown").textContent =
    `${String(Math.floor(evaluation.scenario.minutes_to_close / 60)).padStart(2, "0")}:${String(evaluation.scenario.minutes_to_close % 60).padStart(2, "0")}`;
  $("#accountId").textContent = snapshot.account.account_id.slice(0, 13);
  $("#equity").textContent = money(snapshot.account.equity, 2);
  $("#totalPnl").textContent = signedMoney(snapshot.account.total_pnl);
  $("#totalPnl").className =
    snapshot.account.total_pnl >= 0 ? "positive" : "negative";
  $("#drawdown").textContent =
    `${snapshot.account.max_drawdown_pct.toFixed(2)}%`;
  $("#drawdown").className = "negative";
  $("#optionsLevel").textContent =
    `LEVEL ${snapshot.account.options_trading_level}`;
  $("#feedLabel").textContent =
    snapshot.position.short_leg.quote.feed.toUpperCase();

  const severity = evaluation.urgency.toUpperCase();
  $("#severityBadge").textContent = severity;
  $("#severityBadge").className = `severity ${evaluation.urgency}`;
  const shortStrike = snapshot.position.short_leg.strike;
  const longStrike = snapshot.position.long_leg.strike;
  const strategyName =
    snapshot.position.strategy === "bull_put_credit"
      ? "PUT CREDIT"
      : "CALL CREDIT";
  $("#positionTitle").textContent =
    `${snapshot.position.underlying} ${shortStrike}/${longStrike} ${strategyName}`;
  $("#positionPnl").textContent =
    `ENTRY +${money(snapshot.position.entry_credit * 100)}`;
  $("#legs").innerHTML = [
    snapshot.position.short_leg,
    snapshot.position.long_leg,
  ]
    .map(
      (leg) => `
    <div class="leg ${leg.position_side}">
      <span class="leg-badge">${leg.position_side.toUpperCase()}</span>
      <b>${safe(leg.symbol)}</b>
      <small>${money(leg.quote.bid, 2)} × ${money(leg.quote.ask, 2)}</small>
    </div>`,
    )
    .join("");
  $("#entryDate").textContent = new Date(snapshot.position.opened_at)
    .toLocaleDateString("en-US", { month: "short", day: "2-digit" })
    .toUpperCase();
  $("#airlockTime").textContent = `${evaluation.scenario.minutes_to_close} MIN`;
  $("#expiryDate").textContent = shortDate(
    snapshot.position.short_leg.expiration,
  );
  $("#runwayProgress").style.width =
    `${Math.max(44, Math.min(96, 100 - evaluation.scenario.minutes_to_close / 4))}%`;
  $("#spotLabel").textContent = `SPOT ${money(evaluation.effective_spot, 2)}`;

  $("#spotShiftValue").textContent = signedPct(
    evaluation.scenario.spot_shift_pct,
    1,
  );
  $("#buyingPowerValue").textContent =
    `${evaluation.scenario.buying_power_pct}%`;
  $("#minutesCloseValue").textContent =
    `${evaluation.scenario.minutes_to_close} min`;
  $("#shortDistance").textContent = signedPct(evaluation.short_distance_pct);
  $("#shortDistance").className =
    evaluation.short_distance_pct < 0 ? "negative" : "";
  const hold = evaluation.outcomes.find((outcome) => outcome.action === "HOLD");
  $("#assignmentNotional").textContent = money(hold.assignment_notional);
  $("#optionsBp").textContent = money(evaluation.effective_buying_power);
  $("#gateList").innerHTML = evaluation.gates
    .map(
      (gate) => `
    <div class="gate ${gate.passed ? "" : "fail"}">
      <i></i><b>${safe(gate.label)}</b><span>${safe(gate.detail)}</span>
    </div>`,
    )
    .join("");

  renderPayoff(hold.terminal_states, evaluation.effective_spot);
  renderDecisions();
  renderEquity();
  renderLatestRun();
  $("#chainStatus").textContent = capabilities.audit_chain_valid
    ? `CHAIN VERIFIED · ${capabilities.audit_chain_length} RUNS`
    : "CHAIN FAILED";
  $("#chainStatus").className = capabilities.audit_chain_valid
    ? "positive"
    : "negative";
}

function renderPayoff(points, spot) {
  const width = 650;
  const height = 140;
  const minPnl = Math.min(...points.map((point) => point.pnl), -1);
  const maxPnl = Math.max(...points.map((point) => point.pnl), 1);
  const minSpot = Math.min(...points.map((point) => point.spot));
  const maxSpot = Math.max(...points.map((point) => point.spot));
  const x = (value) =>
    18 + ((value - minSpot) / (maxSpot - minSpot)) * (width - 36);
  const y = (value) =>
    height - 17 - ((value - minPnl) / (maxPnl - minPnl)) * (height - 34);
  const path = points
    .map(
      (point, index) => `${index ? "L" : "M"}${x(point.spot)},${y(point.pnl)}`,
    )
    .join(" ");
  const zeroY = y(0);
  const spotX = Math.max(18, Math.min(width - 18, x(spot)));
  $("#payoffChart").innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <defs><linearGradient id="payoffFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d9ff43" stop-opacity=".45"/><stop offset="1" stop-color="#d9ff43" stop-opacity="0"/></linearGradient></defs>
      <line x1="0" y1="${zeroY}" x2="${width}" y2="${zeroY}" stroke="rgba(17,19,15,.18)" stroke-dasharray="4 5"/>
      <path d="${path} L${width - 18},${height - 12} L18,${height - 12} Z" fill="url(#payoffFill)"/>
      <path d="${path}" fill="none" stroke="#11130f" stroke-width="3" vector-effect="non-scaling-stroke"/>
      <line x1="${spotX}" y1="5" x2="${spotX}" y2="${height - 8}" stroke="#ff6534" stroke-width="2" stroke-dasharray="3 4"/>
      <circle cx="${spotX}" cy="${y(interpolatePnl(points, spot))}" r="5" fill="#ff6534" stroke="#f3f1e8" stroke-width="3" vector-effect="non-scaling-stroke"/>
    </svg>`;
}

function interpolatePnl(points, spot) {
  if (spot <= points[0].spot) return points[0].pnl;
  if (spot >= points[points.length - 1].spot)
    return points[points.length - 1].pnl;
  for (let index = 1; index < points.length; index += 1) {
    if (spot <= points[index].spot) {
      const left = points[index - 1];
      const right = points[index];
      const ratio = (spot - left.spot) / (right.spot - left.spot);
      return left.pnl + ratio * (right.pnl - left.pnl);
    }
  }
  return 0;
}

function renderDecisions() {
  const { evaluation } = state;
  $("#decisionGrid").innerHTML = evaluation.outcomes
    .map((outcome) => {
      const recommended = outcome.action === evaluation.policy_action;
      const metricLabel =
        outcome.action === "HOLD"
          ? "MAX PROFIT"
          : outcome.action === "CLOSE"
            ? "LOCKED P&L"
            : "NEW MAX LOSS";
      const metricValue =
        outcome.action === "ROLL"
          ? money(outcome.locked_or_max_pnl)
          : signedMoney(outcome.locked_or_max_pnl);
      return `
      <article class="decision-card ${recommended ? "recommended" : ""} ${outcome.allowed ? "" : "blocked"}">
        <div class="decision-card-head">
          <strong>${outcome.action}</strong>
          <span>${outcome.allowed ? (recommended ? "POLICY PICK" : "ELIGIBLE") : "BLOCKED"}</span>
        </div>
        <h3>${safe(outcome.headline)}</h3>
        <p>${safe(outcome.detail)}</p>
        <div class="decision-metrics">
          <span>RISK SCORE<b>${outcome.risk_score}/100</b></span>
          <span>${metricLabel}<b>${metricValue}</b></span>
        </div>
        ${outcome.blockers.length ? `<div class="blocker">× ${safe(outcome.blockers[0])}</div>` : ""}
      </article>`;
    })
    .join("");
}

function renderEquity() {
  const curve = state.snapshot.equity_curve;
  const svg = $("#equityChart");
  const width = 620;
  const height = 180;
  const values = curve.map((point) => point.equity);
  const min = Math.min(...values) - 50;
  const max = Math.max(...values) + 50;
  const x = (index) =>
    10 + (index / Math.max(curve.length - 1, 1)) * (width - 20);
  const y = (value) =>
    height - 18 - ((value - min) / (max - min)) * (height - 36);
  const path = curve
    .map((point, index) => `${index ? "L" : "M"}${x(index)},${y(point.equity)}`)
    .join(" ");
  svg.innerHTML = `
    <defs><linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#367647" stop-opacity=".3"/><stop offset="1" stop-color="#367647" stop-opacity="0"/></linearGradient></defs>
    <line x1="0" y1="${y(state.snapshot.account.starting_equity)}" x2="${width}" y2="${y(state.snapshot.account.starting_equity)}" stroke="rgba(17,19,15,.18)" stroke-dasharray="4 5"/>
    <path d="${path} L${width - 10},${height - 10} L10,${height - 10} Z" fill="url(#equityFill)"/>
    <path d="${path}" fill="none" stroke="#11130f" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
    ${curve.map((point, index) => `<circle cx="${x(index)}" cy="${y(point.equity)}" r="3.5" fill="${index === curve.length - 1 ? "#ff6534" : "#11130f"}"/>`).join("")}`;
  $("#equityDelta").textContent = signedMoney(state.snapshot.account.total_pnl);
  $("#equityDelta").className =
    state.snapshot.account.total_pnl >= 0 ? "positive" : "negative";
}

function renderLatestRun() {
  const run = state.latestRun;
  if (!run) {
    $("#agentAction").textContent = state.evaluation.policy_action;
    $("#agentReasoning").textContent =
      `Policy preview: ${state.evaluation.policy_action}. Run the airlock to ask the configured model and record the decision.`;
    $("#agentModel").textContent = state.capabilities.llm_configured
      ? "MODEL READY"
      : "DETERMINISTIC FALLBACK";
    $("#chainHash").textContent = "GENESIS";
    return;
  }
  if (!sameScenario(run.evaluation.scenario, state.evaluation.scenario)) {
    $("#agentAction").textContent = state.evaluation.policy_action;
    $("#agentReasoning").textContent =
      `Scenario changed. Policy preview: ${state.evaluation.policy_action}. Run the airlock to record this decision.`;
    $("#agentModel").textContent = "PENDING NEW RUN";
    $("#chainHash").textContent = run.record_hash.slice(0, 24);
    return;
  }
  $("#agentAction").textContent = run.decision.action;
  $("#agentReasoning").textContent = run.decision.thesis;
  $("#agentModel").textContent =
    `${run.decision.source.toUpperCase()} · ${(run.decision.confidence * 100).toFixed(0)}%`;
  $("#chainHash").textContent = run.record_hash.slice(0, 24);
  renderReceipt(run.execution);
}

function sameScenario(left, right) {
  return Boolean(
    left &&
      right &&
      Number(left.spot_shift_pct) === Number(right.spot_shift_pct) &&
      Number(left.buying_power_pct) === Number(right.buying_power_pct) &&
      Number(left.minutes_to_close) === Number(right.minutes_to_close) &&
      left.as_of_date === right.as_of_date,
  );
}

function renderReceipt(receipt) {
  $("#receiptStatus").textContent = receipt.status.toUpperCase();
  $("#receiptStatus").className =
    receipt.status === "failed" || receipt.status === "blocked"
      ? "negative"
      : ["submitted", "simulated", "recovered"].includes(receipt.status)
        ? "positive"
        : "";
  const lines = [];
  if (receipt.command_preview) lines.push(`$ ${receipt.command_preview}`);
  lines.push(`\nstatus: ${receipt.status}`);
  if (receipt.client_order_id)
    lines.push(`client_order_id: ${receipt.client_order_id}`);
  if (receipt.order_id) lines.push(`order_id: ${receipt.order_id}`);
  if (receipt.broker_status)
    lines.push(`broker_status: ${receipt.broker_status}`);
  lines.push(`detail: ${receipt.detail}`);
  $("#receiptOutput").textContent = lines.join("\n");
}

async function loadRuns() {
  state.runs = await request("/api/runs?limit=8");
  renderLedger();
}

function renderLedger() {
  if (!state.runs.length) return;
  $("#ledgerTable").innerHTML = `
    <div class="ledger-row header"><span>RUN</span><span>ACTION</span><span>URGENCY</span><span>EXECUTION</span><span>HASH</span></div>
    ${state.runs
      .map(
        (run) => `
      <div class="ledger-row">
        <code>${safe(run.id)}</code>
        <span class="ledger-action">${run.decision.action}</span>
        <span>${run.evaluation.urgency.toUpperCase()}</span>
        <span>${run.execution.status.toUpperCase()}</span>
        <code>${run.record_hash.slice(0, 22)}</code>
      </div>`,
      )
      .join("")}`;
}

function scheduleScenario() {
  $("#spotShiftValue").textContent = signedPct(
    Number($("#spotShift").value),
    1,
  );
  $("#buyingPowerValue").textContent = `${$("#buyingPower").value}%`;
  $("#minutesCloseValue").textContent = `${$("#minutesClose").value} min`;
  clearTimeout(state.debounce);
  state.debounce = setTimeout(() => bootstrap().catch(showError), 130);
}

async function runAgent() {
  const button = $("#runAgent");
  button.disabled = true;
  button.querySelector("span").textContent = "RUNNING";
  try {
    const run = await request("/api/runs", {
      method: "POST",
      body: JSON.stringify({
        scenario: scenario(),
        execute: $("#executeToggle").checked,
      }),
    });
    state.latestRun = run;
    renderLatestRun();
    await loadRuns();
    showToast(`${run.decision.action} recorded · ${run.execution.status}`);
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "RUN AIRLOCK";
  }
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 3200);
}

function showError(error) {
  console.error(error);
  showToast(error.message || "LastSafe encountered an error");
}

function bind() {
  ["#spotShift", "#buyingPower", "#minutesClose"].forEach((selector) => {
    $(selector).addEventListener("input", scheduleScenario);
  });
  $("#resetScenario").addEventListener("click", () => {
    $("#spotShift").value = 0;
    $("#buyingPower").value = 100;
    $("#minutesClose").value = 95;
    scheduleScenario();
  });
  $("#runAgent").addEventListener("click", runAgent);
}

function bindReveals() {
  const elements = document.querySelectorAll(
    ".workspace, .decision-section, .evidence-section, .ledger-section",
  );
  elements.forEach((element) => element.classList.add("reveal"));
  if (!("IntersectionObserver" in window)) {
    elements.forEach((element) => element.classList.add("visible"));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "0px 0px -60px", threshold: 0.08 },
  );
  elements.forEach((element) => observer.observe(element));
}

bind();
bindReveals();
Promise.all([bootstrap(), loadRuns()]).catch(showError);
