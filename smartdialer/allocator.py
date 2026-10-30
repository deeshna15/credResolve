"""Call Allocator and Telephony Event Processor.

Enforces the non-bypassable architecture pipeline:
Campaign > Pacing Engine > Safety Controller > Call Allocator > Telecom Provider
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
import logging
from typing import Any, Callable, Dict, List, Optional
import uuid

from smartdialer.db import Database, utc_now
from smartdialer.models import (
    Agent,
    AgentState,
    Borrower,
    Call,
    CallState,
    PacingMode,
    SafetyAction,
    SafetyEvaluation,
    TelephonyEvent,
)
from smartdialer.telecom import TelecomProvider

logger = logging.getLogger(__name__)


class CallAllocator:
    """Orchestrates outbound call placement and event dispatching.

    GUARANTEE: Calls are ONLY placed if backed by a valid SafetyEvaluation.
    """

    def __init__(
        self,
        db: Database,
        telecom_provider: TelecomProvider,
        worker_id: str = "WORKER_1",
        campaign_id: str = "CAMPAIGN_DEFAULT",
    ):
        self.db = db
        self.provider = telecom_provider
        self.worker_id = worker_id
        self.campaign_id = campaign_id

    def allocate_and_dispatch(
        self,
        safety_eval: SafetyEvaluation,
    ) -> List[Call]:
        """Dispatches outbound calls strictly according to Safety Controller authorization."""
        authorized_count = safety_eval.authorized_calls
        if authorized_count <= 0:
            return []

        mode = safety_eval.active_mode
        dispatched_calls: List[Call] = []

        for _ in range(authorized_count):
            if mode == PacingMode.PROGRESSIVE:
                call = self._dispatch_progressive_call()
            else:
                call = self._dispatch_predictive_call()

            if call:
                dispatched_calls.append(call)
            else:
                # Ran out of available agents or borrowers
                break

        return dispatched_calls

    def _dispatch_progressive_call(self) -> Optional[Call]:
        """Progressive dispatch: Atomically reserves agent, then borrower, then initiates."""
        # 1. Dequeue and reserve next prioritized borrower
        borrower = self.db.fetch_and_reserve_next_borrower(worker_id=self.worker_id)
        if not borrower:
            return None

        # 2. Skills & Risk Routing: High DPD / High Risk routes to Senior Recovery (T3), else T1
        pref_team = "T3" if (borrower.dpd > 60 or borrower.risk_segment == "HIGH") else "T1"
        available_agents = self.db.list_available_agents(limit=10, preferred_team=pref_team)

        reserved_agent: Optional[Agent] = None
        for cand in available_agents:
            if self.db.try_atomic_reserve_agent(
                agent_id=cand.agent_id,
                expected_version=cand.version,
                worker_id=self.worker_id,
            ):
                reserved_agent = cand
                break

        if not reserved_agent:
            # Rollback borrower reservation
            self.db.release_borrower(borrower.borrower_id, call_succeeded=False)
            return None

        # 3. Create call record
        call_id = f"CALL_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        call = Call(
            call_id=call_id,
            campaign_id=self.campaign_id,
            borrower_id=borrower.borrower_id,
            account_id=borrower.account_id,
            phone=borrower.phone,
            agent_id=reserved_agent.agent_id,
            state=CallState.INITIATED,
            mode=PacingMode.PROGRESSIVE,
            vendor_id=self.provider.vendor_id,
            worker_id=self.worker_id,
            lease_expires_at=now + timedelta(seconds=30),
            created_at=now,
            initiated_at=now,
        )
        self.db.create_call(call)

        # 4. Transition agent to DIALING
        self.db.update_agent_state(
            reserved_agent.agent_id,
            AgentState.DIALING,
            current_call_id=call_id,
        )

        # 5. Hand off to telecom provider
        self.provider.initiate_call(
            call_id=call_id,
            phone=borrower.phone,
            event_callback=self.process_telephony_event,
        )

        return call

    def _dispatch_predictive_call(self) -> Optional[Call]:
        """Predictive dispatch: Reserves borrower and initiates call before agent is assigned."""
        borrower = self.db.fetch_and_reserve_next_borrower(worker_id=self.worker_id)
        if not borrower:
            return None

        call_id = f"CALL_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        call = Call(
            call_id=call_id,
            campaign_id=self.campaign_id,
            borrower_id=borrower.borrower_id,
            account_id=borrower.account_id,
            phone=borrower.phone,
            agent_id=None,  # Not assigned yet in predictive mode
            state=CallState.INITIATED,
            mode=PacingMode.PREDICTIVE,
            vendor_id=self.provider.vendor_id,
            worker_id=self.worker_id,
            lease_expires_at=now + timedelta(seconds=30),
            created_at=now,
            initiated_at=now,
        )
        self.db.create_call(call)

        self.provider.initiate_call(
            call_id=call_id,
            phone=borrower.phone,
            event_callback=self.process_telephony_event,
        )

        return call

    def process_telephony_event(self, event: TelephonyEvent) -> None:
        """Processes incoming provider events with idempotency and monotonic guards."""
        # 1. Enforce Event Deduplication
        if not self.db.record_telephony_event(event):
            logger.info(
                f"Duplicate telephony event detected and dropped: {event.call_id} [{event.event_type}]"
            )
            return

        call = self.db.get_call(event.call_id)
        if not call:
            logger.warning(f"Event received for unknown call_id={event.call_id}")
            return

        target_state: Optional[CallState] = None
        if event.event_type == "INITIATED":
            target_state = CallState.INITIATED
        elif event.event_type == "RINGING":
            target_state = CallState.RINGING
        elif event.event_type == "ANSWERED":
            target_state = CallState.ANSWERED
        elif event.event_type == "COMPLETED":
            target_state = CallState.COMPLETED
        elif event.event_type == "FAILED":
            target_state = CallState.FAILED

        if not target_state:
            return

        # Handle State-Specific Business Logic
        if target_state == CallState.ANSWERED:
            self._handle_answered_event(call, event)
        elif target_state == CallState.COMPLETED:
            self._handle_completed_event(call, event)
        elif target_state == CallState.FAILED:
            self._handle_failed_event(call, event)
        else:
            self.db.transition_call(call.call_id, target_state)

    def _handle_answered_event(self, call: Call, event: TelephonyEvent) -> None:
        """When a call answers: bridge with agent (or progressive assigned agent)."""
        assigned_agent_id = call.agent_id

        # In Predictive mode, find an agent right now using skills-based priority
        if not assigned_agent_id:
            borrower = self.db.get_borrower(call.borrower_id)
            pref_team = "T3" if (borrower and (borrower.dpd > 60 or borrower.risk_segment == "HIGH")) else "T1"
            available_agents = self.db.list_available_agents(limit=5, preferred_team=pref_team)
            for cand in available_agents:
                if self.db.try_atomic_reserve_agent(
                    agent_id=cand.agent_id,
                    expected_version=cand.version,
                    worker_id=self.worker_id,
                ):
                    assigned_agent_id = cand.agent_id
                    break

        if assigned_agent_id:
            # Transition agent to CONNECTED
            self.db.update_agent_state(
                assigned_agent_id,
                AgentState.CONNECTED,
                current_call_id=call.call_id,
            )
            # Transition call to ANSWERED and immediately to CONNECTED
            self.db.transition_call(
                call.call_id, CallState.ANSWERED, agent_id=assigned_agent_id
            )
            self.db.transition_call(
                call.call_id, CallState.CONNECTED, agent_id=assigned_agent_id
            )
        else:
            # NO AGENT AVAILABLE TO HANDLE ANSWERED CALL: COMPLIANCE ABANDON
            logger.error(
                f"COMPLIANCE ABANDON: Call {call.call_id} answered but no available agents found!"
            )
            self.db.transition_call(
                call.call_id,
                CallState.COMPLETED,
                hangup_reason="COMPLIANCE_ABANDONED",
                duration_sec=0.0,
            )
            self.db.release_borrower(call.borrower_id, call_succeeded=False)

    def _handle_completed_event(self, call: Call, event: TelephonyEvent) -> None:
        duration = event.raw_payload.get("duration", 60.0)
        success, code = self.db.transition_call(
            call.call_id,
            CallState.COMPLETED,
            duration_sec=duration,
            hangup_reason=event.raw_payload.get("reason", "NORMAL_HANGUP"),
        )
        if success and call.agent_id:
            # Move agent to WRAP_UP and record duration
            self.db.update_agent_state(
                call.agent_id,
                AgentState.WRAP_UP,
                duration_to_add=duration,
            )

        self.db.release_borrower(call.borrower_id, call_succeeded=True)

    def _handle_failed_event(self, call: Call, event: TelephonyEvent) -> None:
        reason = event.raw_payload.get("reason", "TELECOM_ERROR")
        success, code = self.db.transition_call(
            call.call_id,
            CallState.FAILED,
            hangup_reason=reason,
        )
        # In progressive mode, free up the pre-reserved agent
        if call.agent_id:
            self.db.update_agent_state(call.agent_id, AgentState.AVAILABLE)

        self.db.release_borrower(call.borrower_id, call_succeeded=False)
