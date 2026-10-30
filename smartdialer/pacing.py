"""Pacing Engines for SmartDialer: Progressive Mode and Predictive Mode.

Implements mathematical pacing calculations with comprehensive explainability.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import math
from typing import Any, Dict, Optional

from smartdialer.models import (
    PacingMode,
    PacingRecommendation,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IPacingEngine(ABC):
    @abstractmethod
    def calculate_pacing(
        self,
        available_agents: int,
        connected_agents: int,
        ringing_calls: int,
        queued_borrowers: int,
        answer_rate: float,
        avg_talk_time_sec: float,
        avg_ring_time_sec: float = 15.0,
    ) -> PacingRecommendation:
        pass


class ProgressivePacingEngine(IPacingEngine):
    """Progressive Mode: One available agent -> one outbound call.

    Guarantees deterministic zero-overdial safety.
    """

    def calculate_pacing(
        self,
        available_agents: int,
        connected_agents: int,
        ringing_calls: int,
        queued_borrowers: int,
        answer_rate: float,
        avg_talk_time_sec: float,
        avg_ring_time_sec: float = 15.0,
    ) -> PacingRecommendation:
        # Strict 1:1 rule. We can only initiate as many calls as there are idle agents,
        # minus any calls that are currently already dialing/ringing for them.
        unbound_idle_agents = max(0, available_agents - ringing_calls)
        dial_count = min(unbound_idle_agents, queued_borrowers)

        explanation = (
            f"Progressive Pacing: Strict 1:1 agent-to-call invariant. "
            f"Available agents={available_agents}, Active ringing={ringing_calls}, "
            f"Unbound idle agents={unbound_idle_agents}, Queued borrowers={queued_borrowers} -> "
            f"Paced {dial_count} calls."
        )

        return PacingRecommendation(
            timestamp=utc_now(),
            mode=PacingMode.PROGRESSIVE,
            requested_calls=dial_count,
            formula_inputs={
                "available_agents": available_agents,
                "ringing_calls": ringing_calls,
                "queued_borrowers": queued_borrowers,
                "mode": "PROGRESSIVE_1_TO_1",
            },
            explanation=explanation,
        )


class PredictivePacingEngine(IPacingEngine):
    """Predictive Mode: Calculates outbound call volume ahead of agent availability

    Uses fluid-flow / Little's Law queues:
        Expected Available Agents in Ring Window (T_ring):
            E[A_free] = A_avail + (A_connected * (T_ring / AHT))
        Expected Answers from existing In-Flight Ringing Calls:
            E[C_ans_inflight] = C_ringing * P_answer
        Net Capacity:
            NetCapacity = max(0, E[A_free] - E[C_ans_inflight])
        Target Outbound Calls:
            N = floor((NetCapacity / P_answer) * DampingFactor)
    """

    def __init__(
        self,
        target_abandon_rate: float = 0.03,  # 3% statutory compliance threshold
        confidence_damping: float = 0.85,    # 85% safety damping to prevent burst spikes
        min_answer_rate: float = 0.05,       # Flooring to avoid division by zero
    ):
        self.target_abandon_rate = target_abandon_rate
        self.confidence_damping = confidence_damping
        self.min_answer_rate = min_answer_rate

    def calculate_pacing(
        self,
        available_agents: int,
        connected_agents: int,
        ringing_calls: int,
        queued_borrowers: int,
        answer_rate: float,
        avg_talk_time_sec: float,
        avg_ring_time_sec: float = 15.0,
    ) -> PacingRecommendation:
        # Clamp inputs
        p_ans = max(self.min_answer_rate, min(1.0, answer_rate))
        aht = max(20.0, avg_talk_time_sec)
        t_ring = max(5.0, avg_ring_time_sec)

        # 1. Expected agents freeing up during call setup/ring window
        turnover_rate = t_ring / aht
        expected_freed_agents = min(float(connected_agents), connected_agents * turnover_rate)
        total_expected_agent_capacity = available_agents + expected_freed_agents

        # 2. Expected answers already in flight from existing ringing calls
        expected_inflight_answers = ringing_calls * p_ans

        # 3. Net agent capacity available for new dials
        net_agent_capacity = max(0.0, total_expected_agent_capacity - expected_inflight_answers)

        # 4. Outbound dials required to yield net_agent_capacity answers
        raw_dials = net_agent_capacity / p_ans

        # 5. Apply damping factor to absorb statistical variance
        damped_dials = math.floor(raw_dials * self.confidence_damping)

        # 6. Cannot exceed queued borrowers
        final_dials = max(0, min(damped_dials, queued_borrowers))

        explanation = (
            f"Predictive Pacing Decision: "
            f"Available agents={available_agents}, Connected={connected_agents}. "
            f"At AHT={aht:.1f}s and RingWindow={t_ring:.1f}s, expected {expected_freed_agents:.2f} agents to free up. "
            f"Total capacity={total_expected_agent_capacity:.2f} agents. "
            f"With {ringing_calls} calls ringing and AnswerRate={p_ans:.1%}, expecting {expected_inflight_answers:.2f} answers in-flight. "
            f"Net capacity={net_agent_capacity:.2f}. "
            f"Raw dials needed={raw_dials:.1f}. After {self.confidence_damping:.0%} confidence damping -> "
            f"Initiating {final_dials} calls."
        )

        return PacingRecommendation(
            timestamp=utc_now(),
            mode=PacingMode.PREDICTIVE,
            requested_calls=final_dials,
            formula_inputs={
                "available_agents": available_agents,
                "connected_agents": connected_agents,
                "expected_freed_agents": round(expected_freed_agents, 2),
                "total_expected_agent_capacity": round(total_expected_agent_capacity, 2),
                "ringing_calls": ringing_calls,
                "expected_inflight_answers": round(expected_inflight_answers, 2),
                "net_agent_capacity": round(net_agent_capacity, 2),
                "answer_rate": round(p_ans, 4),
                "avg_talk_time_sec": round(aht, 1),
                "ring_time_sec": round(t_ring, 1),
                "confidence_damping": self.confidence_damping,
                "raw_dials": round(raw_dials, 2),
                "queued_borrowers": queued_borrowers,
            },
            explanation=explanation,
        )
