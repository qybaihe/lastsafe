const state = {
  snapshot: null,
  evaluation: null,
  latestRun: null,
  runs: [],
  capabilities: {},
  debounce: null,
};

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
  $("#agentAction").textContent = run.decision.action;
  $("#agentReasoning").textContent = run.decision.thesis;
  $("#agentModel").textContent =
    `${run.decision.source.toUpperCase()} · ${(run.decision.confidence * 100).toFixed(0)}%`;
  $("#chainHash").textContent = run.record_hash.slice(0, 24);
  renderReceipt(run.execution);
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
