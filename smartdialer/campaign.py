"""Campaign Engine orchestrating the complete dialing cycle:
Campaign > Pacing Engine > Safety Controller > Call Allocator > Telecom Provider
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple

from smartdialer.allocator import CallAllocator
from smartdialer.db import Database, utc_now
from smartdialer.models import (
    AgentState,
    Call,
    CallState,
    PacingMode,
    PacingRecommendation,
    SafetyAction,
    SafetyEvaluation,
)
from smartdialer.pacing import (
    IPacingEngine,
    PredictivePacingEngine,
    ProgressivePacingEngine,
)
from smartdialer.safety import SafetyController
from smartdialer.telecom import TelecomProvider

logger = logging.getLogger(__name__)


@dataclass
class CampaignRunResult:
    timestamp: datetime
    mode: PacingMode
    recommendation: PacingRecommendation
    safety_eval: SafetyEvaluation
    dispatched_calls: List[Call]
    metrics_snapshot: Dict[str, Any]


class CampaignEngine:
    """Manages the lifecycle and execution ticks of an outbound collections campaign."""

    def __init__(
        self,
        db: Database,
        campaign_id: str,
        provider: TelecomProvider,
        mode: PacingMode = PacingMode.PROGRESSIVE,
        target_abandon_rate: float = 0.03,
        confidence_damping: float = 0.85,
        worker_id: str = "CAMPAIGN_WORKER_1",
    ):
        self.db = db
        self.campaign_id = campaign_id
        self.provider = provider
        self.mode = mode

        # Pacing Engines
        self.progressive_engine = ProgressivePacingEngine()
        self.predictive_engine = PredictivePacingEngine(
            target_abandon_rate=target_abandon_rate,
            confidence_damping=confidence_damping,
        )

        # Safety Controller (Non-bypassable boundary)
        self.safety_controller = SafetyController(
            max_compliance_abandon_rate=target_abandon_rate,
        )

        # Call Allocator
        self.allocator = CallAllocator(
            db=self.db,
            telecom_provider=self.provider,
            worker_id=worker_id,
            campaign_id=self.campaign_id,
        )

    def execute_tick(self) -> CampaignRunResult:
        """Executes one dial evaluation cycle through the complete safety pipeline."""
        now = utc_now()
        metrics = self.db.get_operational_metrics()

        available_agents = metrics["available_agents"]
        total_active_agents = metrics["total_agents"]
        connected_agents = metrics["calls"].get("CONNECTED", 0)
        ringing_calls = metrics["ringing_calls"]
        queued_borrowers = self.db.count_available_borrowers()
        rolling_abandon_rate = metrics["abandon_rate"]
        provider_health = self.provider.get_health()

        # Step 1: Pacing Engine computes recommended call volume
        engine: IPacingEngine = (
            self.predictive_engine
            if self.mode == PacingMode.PREDICTIVE
            else self.progressive_engine
        )

        recommendation = engine.calculate_pacing(
            available_agents=available_agents,
            connected_agents=connected_agents,
            ringing_calls=ringing_calls,
            queued_borrowers=queued_borrowers,
            answer_rate=self.provider.answer_rate,
            avg_talk_time_sec=self.provider.avg_talk_time_sec,
            avg_ring_time_sec=self.provider.avg_ring_time_sec,
        )

        # Step 2: Safety Controller evaluates recommendation
        safety_eval = self.safety_controller.evaluate(
            recommendation=recommendation,
            available_agents=available_agents,
            total_active_agents=total_active_agents,
            currently_ringing_calls=ringing_calls,
            rolling_abandon_rate=rolling_abandon_rate,
            provider_health=provider_health,
        )

        # Step 3: Record Safety Decision in audit log
        self.db.record_safety_evaluation(safety_eval)

        # Step 4: Call Allocator dispatches authorized calls
        dispatched = self.allocator.allocate_and_dispatch(safety_eval)

        return CampaignRunResult(
            timestamp=now,
            mode=self.mode,
            recommendation=recommendation,
            safety_eval=safety_eval,
            dispatched_calls=dispatched,
            metrics_snapshot=metrics,
        )
