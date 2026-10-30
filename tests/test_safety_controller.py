"""Tests for the Safety Controller boundary.
"""

from datetime import datetime, timezone
import unittest

from smartdialer.models import (
    PacingMode,
    PacingRecommendation,
    SafetyAction,
)
from smartdialer.safety import SafetyController
from smartdialer.telecom import CircuitState, ProviderHealth


class TestSafetyController(unittest.TestCase):

    def setUp(self):
        self.safety = SafetyController(
            max_compliance_abandon_rate=0.03,
            max_overdial_multiplier=3.0,
            agent_drop_threshold_pct=0.20,
        )

    def test_approve_within_safe_envelope(self):
        rec = PacingRecommendation(
            timestamp=datetime.now(timezone.utc),
            mode=PacingMode.PREDICTIVE,
            requested_calls=15,
            formula_inputs={},
            explanation="Normal pacing",
        )
        eval_res = self.safety.evaluate(
            recommendation=rec,
            available_agents=10,
            total_active_agents=50,
            currently_ringing_calls=20,
            rolling_abandon_rate=0.01,
        )
        self.assertEqual(eval_res.action, SafetyAction.APPROVE)
        self.assertEqual(eval_res.authorized_calls, 15)

    def test_reduce_when_exceeding_max_overdial_envelope(self):
        # 20 active agents * 3.0 max multiplier = 60 max ringing calls allowed.
        # Currently ringing = 50.
        # Max new calls allowed = 60 - 50 = 10.
        # Requested = 30 -> Should REDUCE to 10.
        rec = PacingRecommendation(
            timestamp=datetime.now(timezone.utc),
            mode=PacingMode.PREDICTIVE,
            requested_calls=30,
            formula_inputs={},
            explanation="Aggressive burst",
        )
        eval_res = self.safety.evaluate(
            recommendation=rec,
            available_agents=5,
            total_active_agents=20,
            currently_ringing_calls=50,
            rolling_abandon_rate=0.015,
        )
        self.assertEqual(eval_res.action, SafetyAction.REDUCE)
        self.assertEqual(eval_res.authorized_calls, 10)

    def test_fallback_to_progressive_on_high_abandon_rate(self):
        # Rolling abandon rate is 4.5% (> 3.0% legal limit)
        # MUST force FALLBACK_TO_PROGRESSIVE and cap to available agents
        rec = PacingRecommendation(
            timestamp=datetime.now(timezone.utc),
            mode=PacingMode.PREDICTIVE,
            requested_calls=40,
            formula_inputs={},
            explanation="Overconfident dial",
        )
        eval_res = self.safety.evaluate(
            recommendation=rec,
            available_agents=8,
            total_active_agents=50,
            currently_ringing_calls=10,
            rolling_abandon_rate=0.045,  # Non-compliant!
        )
        self.assertEqual(eval_res.action, SafetyAction.FALLBACK_TO_PROGRESSIVE)
        self.assertEqual(eval_res.active_mode, PacingMode.PROGRESSIVE)
        self.assertEqual(eval_res.authorized_calls, 8)  # Capped strictly to idle agents

    def test_sudden_agent_availability_drop(self):
        # Initial baseline: 100 agents
        rec1 = PacingRecommendation(
            timestamp=datetime.now(timezone.utc),
            mode=PacingMode.PREDICTIVE,
            requested_calls=20,
            formula_inputs={},
            explanation="Baseline",
        )
        self.safety.evaluate(
            recommendation=rec1,
            available_agents=100,
            total_active_agents=100,
            currently_ringing_calls=0,
            rolling_abandon_rate=0.01,
        )

        # 40 agents suddenly disappear (100 -> 60, a 40% sudden drop!)
        rec2 = PacingRecommendation(
            timestamp=datetime.now(timezone.utc),
            mode=PacingMode.PREDICTIVE,
            requested_calls=25,
            formula_inputs={},
            explanation="Next tick",
        )
        eval_res = self.safety.evaluate(
            recommendation=rec2,
            available_agents=10,
            total_active_agents=60,
            currently_ringing_calls=15,
            rolling_abandon_rate=0.01,
        )
        self.assertEqual(eval_res.action, SafetyAction.FALLBACK_TO_PROGRESSIVE)
        self.assertIn("Sudden agent drop", eval_res.reason)

    def test_reject_on_provider_circuit_breaker_open(self):
        health = ProviderHealth(
            vendor_id="PROVIDER_B",
            status="DEGRADED",
            circuit_state=CircuitState.OPEN,
            consecutive_timeouts=4,
            error_rate=0.45,
        )
        rec = PacingRecommendation(
            timestamp=datetime.now(timezone.utc),
            mode=PacingMode.PREDICTIVE,
            requested_calls=20,
            formula_inputs={},
            explanation="Dialing",
        )
        eval_res = self.safety.evaluate(
            recommendation=rec,
            available_agents=10,
            total_active_agents=50,
            currently_ringing_calls=5,
            rolling_abandon_rate=0.01,
            provider_health=health,
        )
        self.assertEqual(eval_res.action, SafetyAction.REJECT)
        self.assertEqual(eval_res.authorized_calls, 0)


if __name__ == "__main__":
    unittest.main()
