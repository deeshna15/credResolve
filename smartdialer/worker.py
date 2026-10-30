"""Distributed Dialer Worker process/thread abstraction.

Simulates multiple independent workers operating concurrently on the same database
and campaign without cross-worker deadlocks or double-allocation.
"""

from __future__ import annotations
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Optional

from smartdialer.allocator import CallAllocator
from smartdialer.db import Database, dt_to_iso, utc_now
from smartdialer.models import PacingMode, SafetyAction, SafetyEvaluation
from smartdialer.telecom import TelecomProvider

logger = logging.getLogger(__name__)


class DialerWorker:
    """Independent worker node executing dial cycles against shared campaign data."""

    def __init__(
        self,
        worker_id: str,
        db: Database,
        provider: TelecomProvider,
        campaign_id: str = "CAMPAIGN_MULTI_WORKER",
    ):
        self.worker_id = worker_id
        self.db = db
        self.provider = provider
        self.campaign_id = campaign_id
        self.allocator = CallAllocator(
            db=self.db,
            telecom_provider=self.provider,
            worker_id=self.worker_id,
            campaign_id=self.campaign_id,
        )
        self.is_alive = True
        self._register_worker()

    def _register_worker(self) -> None:
        now_str = dt_to_iso(utc_now())
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                INSERT INTO workers (worker_id, last_heartbeat, status)
                VALUES (?, ?, 'ACTIVE')
                ON CONFLICT(worker_id) DO UPDATE SET
                    last_heartbeat = excluded.last_heartbeat,
                    status = excluded.status;
            """, (self.worker_id, now_str))
            conn.execute("COMMIT;")

    def heartbeat(self) -> None:
        now_str = dt_to_iso(utc_now())
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                UPDATE workers
                SET last_heartbeat = ?, status = 'ACTIVE'
                WHERE worker_id = ?;
            """, (now_str, self.worker_id))
            conn.execute("COMMIT;")

    def run_batch(self, safety_eval: SafetyEvaluation) -> int:
        """Processes a slice of authorized outbound calls."""
        if not self.is_alive:
            raise RuntimeError(f"Worker {self.worker_id} has crashed and cannot process calls.")

        self.heartbeat()
        calls = self.allocator.allocate_and_dispatch(safety_eval)
        return len(calls)

    def simulate_crash(self) -> None:
        """Simulates sudden worker crash / SIGKILL."""
        self.is_alive = False
        now_str = dt_to_iso(utc_now())
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                UPDATE workers
                SET status = 'CRASHED'
                WHERE worker_id = ?;
            """, (self.worker_id,))
            conn.execute("COMMIT;")
        logger.warning(f"Worker {self.worker_id} CRASHED abruptly.")
