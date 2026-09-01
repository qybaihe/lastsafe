from __future__ import annotations

import json

import httpx

from .config import Settings
from .models import Action, AgentDecision, DeskSnapshot, Evaluation

SYSTEM_PROMPT = """You are LastSafe, an options expiry operations agent.
Choose exactly one action from the deterministic set supplied by the application.
You may choose only an action marked allowed. Never invent a contract, quantity, price, or order.
Prioritize avoiding accidental exercise/assignment and preserve defined risk. Return JSON only:
{"action":"HOLD|CLOSE|ROLL","confidence":0.0,"thesis":"...","evidence":["..."],
"rejected_actions":{"HOLD":"...","CLOSE":"...","ROLL":"..."}}
Keep the thesis under 45 words and cite only supplied facts."""


class DecisionService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def decide(self, snapshot: DeskSnapshot, evaluation: Evaluation) -> AgentDecision:
        if not self.settings.llm_api_key:
            return self._fallback(evaluation)

        compact_context = {
            "paper_account": snapshot.account.paper,
            "equity": snapshot.account.equity,
            "options_buying_power": evaluation.effective_buying_power,
            "underlying": snapshot.position.underlying,
            "strategy": snapshot.position.strategy,
            "effective_spot": evaluation.effective_spot,
            "dte": evaluation.dte,
            "minutes_to_close": evaluation.scenario.minutes_to_close,
            "short_distance_pct": evaluation.short_distance_pct,
            "close_debit": evaluation.close_debit,
            "roll_net_credit": evaluation.roll_net_credit,
            "policy_action": evaluation.policy_action,
            "outcomes": [
                {
                    "action": outcome.action,
                    "allowed": outcome.allowed,
                    "risk_score": outcome.risk_score,
                    "detail": outcome.detail,
                    "blockers": outcome.blockers,
                }
                for outcome in evaluation.outcomes
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.settings.llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.settings.llm_model,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": json.dumps(compact_context)},
                        ],
                    },
                )
                response.raise_for_status()
                raw = response.json()["choices"][0]["message"]["content"]
                payload = json.loads(raw)
                decision = AgentDecision(
                    action=payload["action"],
                    source="llm",
                    model=self.settings.llm_model,
                    confidence=payload["confidence"],
                    thesis=payload["thesis"],
                    evidence=payload.get("evidence", []),
                    rejected_actions=payload.get("rejected_actions", {}),
                )
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._fallback(evaluation)

        allowed = {outcome.action for outcome in evaluation.outcomes if outcome.allowed}
        if decision.action not in allowed:
            fallback = self._fallback(evaluation)
            fallback.policy_override = True
            fallback.thesis = (
                f"The model selected blocked action {decision.action}; deterministic policy "
                f"overrode it with {fallback.action}."
            )
            return fallback
        return decision

    def _fallback(self, evaluation: Evaluation) -> AgentDecision:
        action = Action(evaluation.policy_action)
        selected = next(
            outcome for outcome in evaluation.outcomes if Action(outcome.action) == action
        )
        rejected = {
            str(outcome.action): (
                "; ".join(outcome.blockers)
                if outcome.blockers
                else f"Higher risk score ({outcome.risk_score}) than {action.value}"
            )
            for outcome in evaluation.outcomes
            if Action(outcome.action) != action
        }
        return AgentDecision(
            action=action,
            source="deterministic-policy",
            model="lastsafe-airlock-v1",
            confidence=0.94 if evaluation.urgency == "critical" else 0.82,
            thesis=(
                f"Select {action.value}: {selected.detail} "
                "The language model is unavailable, so deterministic expiry policy "
                "retains authority."
            ),
            evidence=[
                f"DTE {evaluation.dte}; {evaluation.scenario.minutes_to_close} minutes to close",
                f"Short-strike clearance {evaluation.short_distance_pct:+.2f}%",
                f"Options buying power ${evaluation.effective_buying_power:,.0f}",
            ],
            rejected_actions=rejected,
        )
