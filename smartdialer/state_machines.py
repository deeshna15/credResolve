"""Explicit state machines for Agent and Call lifecycles.
Provides validation, transition rules, idempotency detection, and monotonic safeguards.
"""

from __future__ import annotations
import logging
from typing import Dict, Set, Tuple

from smartdialer.models import (
    AgentState,
    CallState,
    CALL_STATE_RANKS,
    TERMINAL_CALL_STATES,
)

logger = logging.getLogger(__name__)


class StateTransitionError(Exception):
    """Raised when an illegal state transition is attempted."""
    pass


class AgentStateMachine:
    """Explicit lifecycle rules for collections agents.

    Lifecycle:
        OFFLINE <-> AVAILABLE <-> RESERVED -> DIALING -> CONNECTED -> WRAP_UP -> AVAILABLE
                        ^                                                    |
                        +------------------- PAUSED <------------------------+
    """

    ALLOWED_TRANSITIONS: Dict[AgentState, Set[AgentState]] = {
        AgentState.OFFLINE: {AgentState.AVAILABLE},
        AgentState.AVAILABLE: {
            AgentState.RESERVED,
            AgentState.DIALING,     # Direct progressive dial assignment
            AgentState.CONNECTED,   # Direct predictive bridge upon answer
            AgentState.PAUSED,
            AgentState.OFFLINE,
        },
        AgentState.RESERVED: {
            AgentState.DIALING,     # Progressive mode: dialing with reserved agent
            AgentState.CONNECTED,   # Predictive mode: bridged immediately on answer
            AgentState.AVAILABLE,   # Aborted reservation / timeout recovery
            AgentState.OFFLINE,
        },
        AgentState.DIALING: {
            AgentState.CONNECTED,   # Call answered
            AgentState.AVAILABLE,   # Call unanswered, busy, or failed
            AgentState.WRAP_UP,     # In case disposition required immediately
            AgentState.OFFLINE,
        },
        AgentState.CONNECTED: {
            AgentState.WRAP_UP,     # Call finished, enter post-call work
            AgentState.AVAILABLE,   # Immediate release if zero wrap-up
            AgentState.OFFLINE,     # Abrupt disconnection
        },
        AgentState.WRAP_UP: {
            AgentState.AVAILABLE,   # Ready for next call
            AgentState.PAUSED,      # Agent steps away after call
            AgentState.OFFLINE,
        },
        AgentState.PAUSED: {
            AgentState.AVAILABLE,
            AgentState.OFFLINE,
        },
    }

    @classmethod
    def can_transition(cls, current_state: AgentState, next_state: AgentState) -> bool:
        if current_state == next_state:
            return True  # Idempotent no-op
        # Any state can transition to OFFLINE if agent disconnects
        if next_state == AgentState.OFFLINE:
            return True
        return next_state in cls.ALLOWED_TRANSITIONS.get(current_state, set())

    @classmethod
    def validate_transition(cls, current_state: AgentState, next_state: AgentState) -> None:
        if not cls.can_transition(current_state, next_state):
            raise StateTransitionError(
                f"Illegal agent transition: cannot move from {current_state.value} to {next_state.value}"
            )


class CallStateMachine:
    """Explicit lifecycle rules for outbound calls.

    Protects against out-of-order events, duplicate events, and late-arriving webhooks.
    """

    ALLOWED_TRANSITIONS: Dict[CallState, Set[CallState]] = {
        CallState.QUEUED: {CallState.RESERVED, CallState.CANCELLED},
        CallState.RESERVED: {CallState.INITIATED, CallState.CANCELLED},
        CallState.INITIATED: {CallState.RINGING, CallState.FAILED, CallState.CANCELLED},
        CallState.RINGING: {CallState.ANSWERED, CallState.FAILED, CallState.CANCELLED},
        CallState.ANSWERED: {CallState.CONNECTED, CallState.COMPLETED, CallState.FAILED},
        CallState.CONNECTED: {CallState.COMPLETED, CallState.FAILED},
        # Terminal states have no forward transitions
        CallState.COMPLETED: set(),
        CallState.FAILED: set(),
        CallState.CANCELLED: set(),
    }

    @classmethod
    def evaluate_transition(
        cls, current_state: CallState, target_state: CallState
    ) -> Tuple[bool, str]:
        """Evaluates a proposed transition.

        Returns:
            (should_apply, reason_code)
            reason_code is one of: "APPLY", "DUPLICATE", "OUT_OF_ORDER", "ILLEGAL"
        """
        # 1. Idempotent check
        if current_state == target_state:
            return False, "DUPLICATE"

        # 2. Terminal lock: terminal states cannot be changed
        if current_state in TERMINAL_CALL_STATES:
            return False, "OUT_OF_ORDER"

        # 3. Monotonic check: events cannot regress backward in rank
        current_rank = CALL_STATE_RANKS[current_state]
        target_rank = CALL_STATE_RANKS[target_state]
        if target_rank < current_rank:
            return False, "OUT_OF_ORDER"

        # 4. Direct allowed transition check
        if target_state in cls.ALLOWED_TRANSITIONS.get(current_state, set()):
            return True, "APPLY"

        # 5. Out-of-order forward skips (e.g. telecom jumps directly from RINGING to COMPLETED, or INITIATED to ANSWERED)
        # We permit forward skips toward terminal states or valid higher ranks with logging
        if target_rank > current_rank:
            return True, "APPLY"

        return False, "ILLEGAL"
