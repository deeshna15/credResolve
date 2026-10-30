"""Safety Controller for SmartDialer.

Positioned strictly between the Pacing Engine and the Call Allocator.
Non-bypassable boundary that enforces statutory compliance, overdial caps,
circuit breaker trips, and graceful fallback to progressive dialing.
"""

from __future__ import annotations
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional, Tuple

from smartdialer.models import (
    PacingMode,
    PacingRecommendation,
    SafetyAction,
    SafetyEvaluation,
)
from smartdialer.telecom import CircuitState, ProviderHealth

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SafetyController:
    """Non-bypassable Safety Controller.

    Decisions:
    - APPROVE: Requested call volume is safe.
    - REDUCE: Volume capped to comply with safety envelopes.
    - REJECT: Overdial risk or system state is unsafe; 0 calls placed.
    - FALLBACK_TO_PROGRESSIVE: Predictive risk detected (abandon rate > threshold or
      abrupt agent loss); forces strict 1:1 progressive dialing.
    """

    def __init__(
        self,
        max_compliance_abandon_rate: float = 0.03,  # 3% maximum abandon threshold
        max_overdial_multiplier: float = 3.0,       # Max concurrent ringing calls per active agent
        agent_drop_threshold_pct: float = 0.20,     # Trigger fallback if >20% agents disappear
    ):
        self.max_compliance_abandon_rate = max_compliance_abandon_rate
        self.max_overdial_multiplier = max_overdial_multiplier
        self.agent_drop_threshold_pct = agent_drop_threshold_pct
        self._last_known_active_agents: Optional[int] = None
        self._last_agent_check_time: Optional[datetime] = None

    def evaluate(
        self,
        recommendation: PacingRecommendation,
        available_agents: int,
        total_active_agents: int,
        currently_ringing_calls: int,
        rolling_abandon_rate: float,
        provider_health: Optional[ProviderHealth] = None,
    ) -> SafetyEvaluation:
        requested = recommendation.requested_calls
        now = utc_now()

        metrics_snapshot = {
            "requested": requested,
            "mode": recommendation.mode.value,
            "available_agents": available_agents,
            "total_active_agents": total_active_agents,
            "currently_ringing_calls": currently_ringing_calls,
            "rolling_abandon_rate": round(rolling_abandon_rate, 4),
            "provider_status": provider_health.status if provider_health else "UNKNOWN",
            "provider_error_rate": round(provider_health.error_rate, 4) if provider_health else 0.0,
        }

        # 1. HARD STOP: Zero Agents in System
        if total_active_agents <= 0:
            return SafetyEvaluation(
                timestamp=now,
                action=SafetyAction.REJECT,
                requested_calls=requested,
                authorized_calls=0,
                reason="Safety Guard: Zero active agents in system. Outbound dialing blocked.",
                active_mode=recommendation.mode,
                metrics_snapshot=metrics_snapshot,
            )

        # 2. PROVIDER OUTAGE / CIRCUIT BREAKER GUARD
        if provider_health and (
            provider_health.circuit_state == CircuitState.OPEN
            or provider_health.consecutive_timeouts >= 3
            or provider_health.error_rate > 0.35
        ):
            return SafetyEvaluation(
                timestamp=now,
                action=SafetyAction.REJECT,
                requested_calls=requested,
                authorized_calls=0,
                reason=(
                    f"Safety Guard: Telecom provider {provider_health.vendor_id} is degraded/circuit open "
                    f"(Timeouts: {provider_health.consecutive_timeouts}, ErrRate: {provider_health.error_rate:.1%}). "
                    f"Halting new calls."
                ),
                active_mode=recommendation.mode,
                metrics_snapshot=metrics_snapshot,
            )

        # 3. SUDDEN AGENT AVAILABILITY DROP GUARD
        # Detect if agents suddenly logged off or disappeared
        if self._last_known_active_agents is not None and self._last_known_active_agents > 0:
            agent_delta = self._last_known_active_agents - total_active_agents
            drop_pct = agent_delta / float(self._last_known_active_agents)
            if drop_pct >= self.agent_drop_threshold_pct:
                logger.warning(
                    f"Agent availability sudden drop detected: {self._last_known_active_agents} -> "
                    f"{total_active_agents} (-{drop_pct:.1%}). Enforcing FALLBACK_TO_PROGRESSIVE."
                )
                self._last_known_active_agents = total_active_agents
                safe_calls = min(available_agents, requested)
                return SafetyEvaluation(
                    timestamp=now,
                    action=SafetyAction.FALLBACK_TO_PROGRESSIVE,
                    requested_calls=requested,
                    authorized_calls=safe_calls,
                    reason=(
                        f"Safety Guard: Sudden agent drop of {drop_pct:.1%} detected "
                        f"({self._last_known_active_agents} to {total_active_agents}). "
                        f"Predictive pacing aborted; falling back to strict 1:1 progressive mode."
                    ),
                    active_mode=PacingMode.PROGRESSIVE,
                    metrics_snapshot=metrics_snapshot,
                )

        self._last_known_active_agents = total_active_agents
        self._last_agent_check_time = now

        # 4. COMPLIANCE ABANDON RATE GUARD (FTC/FCC / RBI Regulations)
        # If predictive mode is requested, but abandon rate exceeds target -> Fallback to Progressive
        if recommendation.mode == PacingMode.PREDICTIVE:
            if rolling_abandon_rate >= self.max_compliance_abandon_rate:
                logger.warning(
                    f"Abandon rate {rolling_abandon_rate:.2%} exceeds compliance limit "
                    f"{self.max_compliance_abandon_rate:.2%}. Forcing FALLBACK_TO_PROGRESSIVE."
                )
                safe_calls = min(available_agents, requested)
                return SafetyEvaluation(
                    timestamp=now,
                    action=SafetyAction.FALLBACK_TO_PROGRESSIVE,
                    requested_calls=requested,
                    authorized_calls=safe_calls,
                    reason=(
                        f"Safety Guard: Rolling abandon rate ({rolling_abandon_rate:.2%}) exceeds "
                        f"statutory limit ({self.max_compliance_abandon_rate:.2%}). "
                        f"Predictive mode suspended; fallback to progressive 1:1 dialing."
                    ),
                    active_mode=PacingMode.PROGRESSIVE,
                    metrics_snapshot=metrics_snapshot,
                )

        # 5. HARD OVERDIAL ENVELOPE CAP
        # Ensure total concurrent in-flight calls never exceed safe physical envelope
        max_allowed_ringing = int(total_active_agents * self.max_overdial_multiplier)
        allowed_new_ringing = max(0, max_allowed_ringing - currently_ringing_calls)

        if requested <= 0:
            return SafetyEvaluation(
                timestamp=now,
                action=SafetyAction.APPROVE,
                requested_calls=0,
                authorized_calls=0,
                reason="Pacing engine requested 0 calls. Approved.",
                active_mode=recommendation.mode,
                metrics_snapshot=metrics_snapshot,
            )

        # In Progressive mode, never exceed available agents
        if recommendation.mode == PacingMode.PROGRESSIVE:
            progressive_cap = max(0, available_agents - currently_ringing_calls)
            if requested > progressive_cap:
                return SafetyEvaluation(
                    timestamp=now,
                    action=SafetyAction.REDUCE,
                    requested_calls=requested,
                    authorized_calls=progressive_cap,
                    reason=(
                        f"Safety Guard: Progressive invariant allows at most 1 call per idle agent. "
                        f"Reduced from {requested} to {progressive_cap}."
                    ),
                    active_mode=PacingMode.PROGRESSIVE,
                    metrics_snapshot=metrics_snapshot,
                )
            return SafetyEvaluation(
                timestamp=now,
                action=SafetyAction.APPROVE,
                requested_calls=requested,
                authorized_calls=requested,
                reason="Progressive request within safe 1:1 envelope. Approved.",
                active_mode=PacingMode.PROGRESSIVE,
                metrics_snapshot=metrics_snapshot,
            )

        # In Predictive mode, check against allowed_new_ringing
        if requested > allowed_new_ringing:
            return SafetyEvaluation(
                timestamp=now,
                action=SafetyAction.REDUCE,
                requested_calls=requested,
                authorized_calls=allowed_new_ringing,
                reason=(
                    f"Safety Guard: Requested {requested} calls exceeds maximum ringing envelope "
                    f"({currently_ringing_calls} active + {requested} > {max_allowed_ringing} max allowed). "
                    f"Reduced to {allowed_new_ringing}."
                ),
                active_mode=PacingMode.PREDICTIVE,
                metrics_snapshot=metrics_snapshot,
            )

        # All checks passed
        return SafetyEvaluation(
            timestamp=now,
            action=SafetyAction.APPROVE,
            requested_calls=requested,
            authorized_calls=requested,
            reason="All safety boundary checks passed (abandon rate, agent delta, capacity). Approved.",
            active_mode=PacingMode.PREDICTIVE,
            metrics_snapshot=metrics_snapshot,
        )
