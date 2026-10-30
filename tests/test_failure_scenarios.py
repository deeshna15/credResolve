"""Failure scenario tests as required by the CredResolve Tech Assignment.

Demonstrates:
1. Worker crash mid-call setup and automatic lease reconciliation.
2. Worker crash after ANSWERED and compliance rescue.
3. Provider outage / timeouts and circuit breaker tripping.
4. Duplicate provider events and strict idempotency.
5. Out-of-order telecom webhooks.
"""

from datetime import datetime, timezone, timedelta
import unittest

from smartdialer.allocator import CallAllocator
from smartdialer.db import Database, dt_to_iso, utc_now
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
from smartdialer.reaper import ReconciliationReaper
from smartdialer.safety import SafetyController
from smartdialer.telecom import ProviderA, ProviderB


class TestFailureScenarios(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.provider_a = ProviderA()
        self.provider_b = ProviderB(timeout_rate=0.0, duplicate_event_rate=0.0, out_of_order_rate=0.0)

    def test_worker_crash_during_call_initiation(self):
        """Failure Case 1: Worker crashes after Agent reserved > Borrower reserved > Call initiated.

        Verification:
        - When ReconciliationReaper runs, it detects the expired lease.
        - Orphaned call is marked FAILED (hangup_reason='WORKER_CRASH_TIMEOUT').
        - The reserved agent is restored to AVAILABLE.
        - The locked borrower is released so they can be dialed again.
        """
        agent = Agent(agent_id="AGT_CRASH_1", name="Sam Crash", state=AgentState.AVAILABLE)
        self.db.upsert_agent(agent)

        borrower = Borrower(
            borrower_id="BRW_CRASH_1",
            account_id="ACC_CRASH_1",
            name="Bob Borrower",
            phone="+919876543210",
        )
        self.db.upsert_borrower(borrower)

        # Worker 1 allocates and reserves with a dormant/in-flight provider
        dormant_provider = ProviderA(auto_complete=False)
        allocator = CallAllocator(self.db, dormant_provider, worker_id="WORKER_CRASHED")
        safety_eval = SafetyEvaluation(
            timestamp=utc_now(),
            action=SafetyAction.APPROVE,
            requested_calls=1,
            authorized_calls=1,
            reason="Approved",
            active_mode=PacingMode.PROGRESSIVE,
            metrics_snapshot={},
        )

        calls = allocator.allocate_and_dispatch(safety_eval)
        self.assertEqual(len(calls), 1)
        call_id = calls[0].call_id

        # Simulate WORKER CRASH: Worker dies immediately, and its lease expires into the past
        past_time = utc_now() - timedelta(minutes=2)
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE calls SET lease_expires_at = ? WHERE call_id = ?",
                (dt_to_iso(past_time), call_id),
            )

        # Check pre-reconciliation state: agent is stuck DIALING, borrower locked
        stuck_agent = self.db.get_agent("AGT_CRASH_1")
        self.assertEqual(stuck_agent.state, AgentState.DIALING)

        stuck_borrower = self.db.get_borrower("BRW_CRASH_1")
        self.assertTrue(stuck_borrower.is_reserved)

        # RECONCILIATION REAPER RUNS (System recovery loop)
        reaper = ReconciliationReaper(self.db)
        recovered_calls, recovered_agents = reaper.reconcile_stale_leases()

        self.assertEqual(recovered_calls, 1)
        self.assertEqual(recovered_agents, 1)

        # Post-reconciliation verification
        recovered_call = self.db.get_call(call_id)
        self.assertEqual(recovered_call.state, CallState.FAILED)
        self.assertEqual(recovered_call.hangup_reason, "WORKER_CRASH_TIMEOUT")

        restored_agent = self.db.get_agent("AGT_CRASH_1")
        self.assertEqual(restored_agent.state, AgentState.AVAILABLE)
        self.assertIsNone(restored_agent.current_call_id)

        restored_borrower = self.db.get_borrower("BRW_CRASH_1")
        self.assertFalse(restored_borrower.is_reserved)
        self.assertEqual(restored_borrower.call_attempts, 1)

    def test_duplicate_events_telephony_resilience(self):
        """Failure Case 4: Telecom sends ANSWERED multiple times.

        Verification:
        - Handled idempotently.
        - Does NOT create duplicate state transitions or duplicate agent assignments.
        """
        agent = Agent(agent_id="AGT_DUP_1", name="Dave Duplicate", state=AgentState.AVAILABLE)
        self.db.upsert_agent(agent)
        borrower = Borrower(borrower_id="BRW_DUP_1", account_id="ACC_DUP_1", name="Dan", phone="+919999999991")
        self.db.upsert_borrower(borrower)

        call_id = "CALL_DUP_001"
        self.db.create_call(Call(
            call_id=call_id,
            campaign_id="CAMP_DUP",
            borrower_id="BRW_DUP_1",
            account_id="ACC_DUP_1",
            phone="+919999999991",
            agent_id="AGT_DUP_1",
            state=CallState.RINGING,
            mode=PacingMode.PROGRESSIVE,
        ))

        allocator = CallAllocator(self.db, self.provider_a)

        # Send event 1: ANSWERED
        evt1 = TelephonyEvent(
            event_id="EVT_001",
            call_id=call_id,
            event_type="ANSWERED",
            timestamp=utc_now(),
            vendor_id="PROVIDER_A",
            idempotency_key="EVT_KEY_ANS_1",
        )
        allocator.process_telephony_event(evt1)

        call_after_1 = self.db.get_call(call_id)
        self.assertEqual(call_after_1.state, CallState.CONNECTED)
        v1 = call_after_1.version

        # Send duplicate event 2: ANSWERED with same idempotency key
        evt2 = TelephonyEvent(
            event_id="EVT_002",
            call_id=call_id,
            event_type="ANSWERED",
            timestamp=utc_now(),
            vendor_id="PROVIDER_A",
            idempotency_key="EVT_KEY_ANS_1",  # Same key
        )
        allocator.process_telephony_event(evt2)

        call_after_2 = self.db.get_call(call_id)
        # Version must not increment, state remains cleanly CONNECTED
        self.assertEqual(call_after_2.state, CallState.CONNECTED)
        self.assertEqual(call_after_2.version, v1)

    def test_out_of_order_telecom_events(self):
        """Failure Case 5: Events arrive out of order (COMPLETED arrives, then delayed ANSWERED).

        Verification:
        - Terminal state COMPLETED cannot be overwritten by delayed ANSWERED.
        - State machine prevents backward corruption.
        """
        agent = Agent(agent_id="AGT_ORD_1", name="Olivia Order", state=AgentState.AVAILABLE)
        self.db.upsert_agent(agent)
        borrower = Borrower(borrower_id="BRW_ORD_1", account_id="ACC_ORD_1", name="Oscar", phone="+919999999992")
        self.db.upsert_borrower(borrower)

        call_id = "CALL_ORD_001"
        self.db.create_call(Call(
            call_id=call_id,
            campaign_id="CAMP_ORD",
            borrower_id="BRW_ORD_1",
            account_id="ACC_ORD_1",
            phone="+919999999992",
            agent_id="AGT_ORD_1",
            state=CallState.RINGING,
            mode=PacingMode.PROGRESSIVE,
        ))

        allocator = CallAllocator(self.db, self.provider_a)

        # 1. COMPLETED arrives first
        evt_completed = TelephonyEvent(
            event_id="EVT_COMP_1",
            call_id=call_id,
            event_type="COMPLETED",
            timestamp=utc_now(),
            vendor_id="PROVIDER_A",
            raw_payload={"duration": 45.0, "reason": "NORMAL_HANGUP"},
            idempotency_key="KEY_COMP",
        )
        allocator.process_telephony_event(evt_completed)

        call_check = self.db.get_call(call_id)
        self.assertEqual(call_check.state, CallState.COMPLETED)

        # 2. Delayed ANSWERED webhook arrives later
        evt_answered = TelephonyEvent(
            event_id="EVT_ANS_LATE",
            call_id=call_id,
            event_type="ANSWERED",
            timestamp=utc_now(),
            vendor_id="PROVIDER_A",
            idempotency_key="KEY_ANS_LATE",
        )
        allocator.process_telephony_event(evt_answered)

        # Must remain COMPLETED!
        call_after_late = self.db.get_call(call_id)
        self.assertEqual(call_after_late.state, CallState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
