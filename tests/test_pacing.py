"""Tests for Progressive and Predictive Pacing Engines.
"""

import unittest
from smartdialer.models import PacingMode
from smartdialer.pacing import ProgressivePacingEngine, PredictivePacingEngine


class TestPacingEngines(unittest.TestCase):

    def test_progressive_pacing_strict_1_to_1(self):
        engine = ProgressivePacingEngine()

        # 50 available agents, 0 ringing, 100 queued borrowers -> 50 calls
        rec = engine.calculate_pacing(
            available_agents=50,
            connected_agents=20,
            ringing_calls=0,
            queued_borrowers=100,
            answer_rate=0.20,
            avg_talk_time_sec=120.0,
        )
        self.assertEqual(rec.requested_calls, 50)
        self.assertEqual(rec.mode, PacingMode.PROGRESSIVE)

        # 50 available agents, but 10 are already ringing -> 40 calls
        rec2 = engine.calculate_pacing(
            available_agents=50,
            connected_agents=20,
            ringing_calls=10,
            queued_borrowers=100,
            answer_rate=0.20,
            avg_talk_time_sec=120.0,
        )
        self.assertEqual(rec2.requested_calls, 40)

        # 50 available agents, but only 5 queued borrowers -> 5 calls
        rec3 = engine.calculate_pacing(
            available_agents=50,
            connected_agents=20,
            ringing_calls=0,
            queued_borrowers=5,
            answer_rate=0.20,
            avg_talk_time_sec=120.0,
        )
        self.assertEqual(rec3.requested_calls, 5)

    def test_predictive_pacing_formula_and_explainability(self):
        engine = PredictivePacingEngine(confidence_damping=0.85)

        # Example Scenario:
        # 5 available agents, 20 connected agents (AHT = 120s, Ring = 15s).
        # Expected freeing agents = 20 * (15/120) = 2.5 agents.
        # Total expected capacity = 5 + 2.5 = 7.5 agents.
        # In-flight ringing = 5 calls at 20% answer rate = 1.0 expected answer.
        # Net capacity = 7.5 - 1.0 = 6.5.
        # Raw dials needed = 6.5 / 0.20 = 32.5.
        # Damped dials = floor(32.5 * 0.85) = floor(27.625) = 27 calls.
        rec = engine.calculate_pacing(
            available_agents=5,
            connected_agents=20,
            ringing_calls=5,
            queued_borrowers=100,
            answer_rate=0.20,
            avg_talk_time_sec=120.0,
            avg_ring_time_sec=15.0,
        )

        self.assertEqual(rec.requested_calls, 27)
        self.assertEqual(rec.mode, PacingMode.PREDICTIVE)
        self.assertIn("Predictive Pacing Decision", rec.explanation)
        self.assertIn("raw_dials", rec.formula_inputs)
        self.assertIn("net_agent_capacity", rec.formula_inputs)


if __name__ == "__main__":
    unittest.main()
