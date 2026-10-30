"""Tests for Agent and Call explicit state machines.
"""

import unittest
from smartdialer.models import AgentState, CallState
from smartdialer.state_machines import (
    AgentStateMachine,
    CallStateMachine,
    StateTransitionError,
)


class TestStateMachines(unittest.TestCase):

    def test_valid_agent_lifecycle(self):
        # AVAILABLE -> RESERVED -> DIALING -> CONNECTED -> WRAP_UP -> AVAILABLE
        current = AgentState.AVAILABLE
        self.assertTrue(AgentStateMachine.can_transition(current, AgentState.RESERVED))
        AgentStateMachine.validate_transition(current, AgentState.RESERVED)

        current = AgentState.RESERVED
        self.assertTrue(AgentStateMachine.can_transition(current, AgentState.DIALING))

        current = AgentState.DIALING
        self.assertTrue(AgentStateMachine.can_transition(current, AgentState.CONNECTED))

        current = AgentState.CONNECTED
        self.assertTrue(AgentStateMachine.can_transition(current, AgentState.WRAP_UP))

        current = AgentState.WRAP_UP
        self.assertTrue(AgentStateMachine.can_transition(current, AgentState.AVAILABLE))

    def test_invalid_agent_transitions(self):
        # Cannot jump from OFFLINE directly to CONNECTED
        with self.assertRaises(StateTransitionError):
            AgentStateMachine.validate_transition(AgentState.OFFLINE, AgentState.CONNECTED)

        # Cannot jump from DIALING directly to RESERVED
        with self.assertRaises(StateTransitionError):
            AgentStateMachine.validate_transition(AgentState.DIALING, AgentState.RESERVED)

    def test_call_lifecycle_and_monotonicity(self):
        # Clean forward progression
        apply, reason = CallStateMachine.evaluate_transition(CallState.QUEUED, CallState.INITIATED)
        self.assertTrue(apply)
        self.assertEqual(reason, "APPLY")

        apply, reason = CallStateMachine.evaluate_transition(CallState.INITIATED, CallState.RINGING)
        self.assertTrue(apply)

        apply, reason = CallStateMachine.evaluate_transition(CallState.RINGING, CallState.ANSWERED)
        self.assertTrue(apply)

        apply, reason = CallStateMachine.evaluate_transition(CallState.ANSWERED, CallState.CONNECTED)
        self.assertTrue(apply)

        apply, reason = CallStateMachine.evaluate_transition(CallState.CONNECTED, CallState.COMPLETED)
        self.assertTrue(apply)

    def test_duplicate_telecom_events(self):
        # Telecom sends ANSWERED twice
        apply, reason = CallStateMachine.evaluate_transition(CallState.ANSWERED, CallState.ANSWERED)
        self.assertFalse(apply)
        self.assertEqual(reason, "DUPLICATE")

    def test_out_of_order_telecom_events(self):
        # Call is already COMPLETED, delayed ANSWERED webhook arrives
        apply, reason = CallStateMachine.evaluate_transition(CallState.COMPLETED, CallState.ANSWERED)
        self.assertFalse(apply)
        self.assertEqual(reason, "OUT_OF_ORDER")

        # Call is RINGING, stale QUEUED arrives
        apply, reason = CallStateMachine.evaluate_transition(CallState.RINGING, CallState.QUEUED)
        self.assertFalse(apply)
        self.assertEqual(reason, "OUT_OF_ORDER")


if __name__ == "__main__":
    unittest.main()
