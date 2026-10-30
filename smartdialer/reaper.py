"""Reconciliation Reaper for crashed workers, orphaned leases, and stale state recovery.
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
import logging
from typing import List, Tuple

from smartdialer.db import Database, utc_now
from smartdialer.models import AgentState, CallState, Call

logger = logging.getLogger(__name__)


class ReconciliationReaper:
    """Detects and reconciles orphaned calls and agents when workers crash or networks fail."""

    def __init__(self, db: Database, wrap_up_timeout_sec: float = 30.0):
        self.db = db
        self.wrap_up_timeout_sec = wrap_up_timeout_sec

    def reconcile_stale_leases(self) -> Tuple[int, int]:
        """Scans for expired worker leases and restores system consistency.

        Returns (recovered_calls_count, recovered_agents_count).
        """
        expired_calls = self.db.get_expired_leased_calls()
        recovered_calls = 0
        recovered_agents = 0

        for call in expired_calls:
            logger.warning(
                f"Reaper: Recovering orphaned call {call.call_id} in state {call.state.value} "
                f"(Worker {call.worker_id} lease expired)"
            )

            if call.state in (CallState.RESERVED, CallState.INITIATED, CallState.RINGING):
                # Worker crashed before call connected
                self.db.transition_call(
                    call.call_id,
                    CallState.FAILED,
                    hangup_reason="WORKER_CRASH_TIMEOUT",
                )
                # Release reserved agent if assigned
                if call.agent_id:
                    self.db.update_agent_state(call.agent_id, AgentState.AVAILABLE)
                    recovered_agents += 1

                # Unlock borrower for future campaign retry
                self.db.release_borrower(call.borrower_id, call_succeeded=False)
                recovered_calls += 1

            elif call.state == CallState.ANSWERED and not call.agent_id:
                # Worker crashed right after ANSWERED without bridging agent!
                # Compliance emergency: try immediate bridge or record abandon
                available = self.db.list_available_agents(limit=1)
                if available and self.db.try_atomic_reserve_agent(
                    available[0].agent_id, available[0].version, "REAPER"
                ):
                    self.db.update_agent_state(
                        available[0].agent_id,
                        AgentState.CONNECTED,
                        current_call_id=call.call_id,
                    )
                    self.db.transition_call(
                        call.call_id, CallState.CONNECTED, agent_id=available[0].agent_id
                    )
                    logger.info(f"Reaper: Rescued answered call {call.call_id} by assigning agent {available[0].agent_id}")
                else:
                    self.db.transition_call(
                        call.call_id,
                        CallState.COMPLETED,
                        hangup_reason="COMPLIANCE_ABANDONED",
                    )
                    self.db.release_borrower(call.borrower_id, call_succeeded=False)
                    logger.error(f"Reaper: Compliance abandon for answered call {call.call_id} after worker crash.")
                recovered_calls += 1

        # Check for agents stuck in WRAP_UP
        recovered_agents += self._reconcile_stale_wrap_ups()
        return recovered_calls, recovered_agents

    def _reconcile_stale_wrap_ups(self) -> int:
        """Restores agents lingering in WRAP_UP beyond acceptable limits."""
        cutoff = utc_now() - timedelta(seconds=self.wrap_up_timeout_sec)
        recovered = 0
        with self.db.get_connection() as conn:
            cur = conn.execute(
                "SELECT agent_id FROM agents WHERE state = 'WRAP_UP' AND updated_at < ?;",
                (cutoff.isoformat(),),
            )
            stale_agents = [row["agent_id"] for row in cur.fetchall()]

        for agent_id in stale_agents:
            self.db.update_agent_state(agent_id, AgentState.AVAILABLE)
            logger.info(f"Reaper: Auto-wrapped agent {agent_id} back to AVAILABLE.")
            recovered += 1

        return recovered
