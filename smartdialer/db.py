"""Database repository and concurrency engine for SmartDialer.

Uses SQLite with Write-Ahead Logging (WAL) and row-level atomic Compare-And-Swap (CAS)
with versioning to ensure zero double-booking across concurrent workers.
"""

from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import logging
import sqlite3
import threading
from typing import Any, Dict, Generator, List, Optional, Tuple
import uuid

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
from smartdialer.state_machines import AgentStateMachine, CallStateMachine

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


class Database:
    """Thread-safe transactional database connection pool with atomic CAS operations."""

    def __init__(self, db_path: str = ":memory:"):
        self._lock = threading.RLock()
        self._is_memory = (db_path == ":memory:")
        if self._is_memory:
            self._mem_id = uuid.uuid4().hex
            self.db_path = f"file:mem_{self._mem_id}?mode=memory&cache=shared"
            # Keep one anchor connection open so SQLite shared in-memory DB persists
            self._anchor_conn = sqlite3.connect(self.db_path, uri=True, check_same_thread=False)
        else:
            self.db_path = db_path
            self._anchor_conn = None

        self._init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            conn = sqlite3.connect(
                self.db_path,
                timeout=10.0,
                check_same_thread=False,
                uri=self._is_memory,
                isolation_level=None,  # Autocommit mode, we control transactions explicitly
            )
            conn.row_factory = sqlite3.Row
            try:
                if not self._is_memory:
                    conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA busy_timeout = 5000;")
                conn.execute("PRAGMA synchronous = NORMAL;")
                yield conn
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            # Agents table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    team TEXT NOT NULL DEFAULT 'T1',
                    skills TEXT NOT NULL DEFAULT 'GENERAL',
                    current_call_id TEXT,
                    reserved_by TEXT,
                    reserved_at TEXT,
                    updated_at TEXT NOT NULL,
                    total_calls_handled INTEGER DEFAULT 0,
                    total_talk_time_sec REAL DEFAULT 0.0
                );
            """)

            # Borrowers table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS borrowers (
                    borrower_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    dpd INTEGER DEFAULT 0,
                    outstanding_amount REAL DEFAULT 0.0,
                    risk_segment TEXT DEFAULT 'MEDIUM',
                    priority INTEGER DEFAULT 1,
                    call_attempts INTEGER DEFAULT 0,
                    last_call_at TEXT,
                    is_reserved INTEGER DEFAULT 0,
                    reserved_by TEXT
                );
            """)

            # Calls table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    borrower_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    agent_id TEXT,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    vendor_id TEXT,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    initiated_at TEXT,
                    ringing_at TEXT,
                    answered_at TEXT,
                    connected_at TEXT,
                    ended_at TEXT,
                    duration_sec REAL DEFAULT 0.0,
                    hangup_reason TEXT,
                    retry_count INTEGER DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1
                );
            """)

            # Telephony events audit log (idempotency enforcement)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telephony_events (
                    event_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    vendor_id TEXT NOT NULL,
                    raw_payload TEXT,
                    idempotency_key TEXT UNIQUE,
                    processed INTEGER DEFAULT 0
                );
            """)

            # Safety Controller decisions audit log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS safety_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    requested_calls INTEGER NOT NULL,
                    authorized_calls INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    active_mode TEXT NOT NULL,
                    metrics_snapshot TEXT NOT NULL
                );
            """)

            # Workers heartbeat table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    last_heartbeat TEXT NOT NULL,
                    status TEXT NOT NULL
                );
            """)

            # Indices for rapid querying
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_state ON agents(state);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_borrowers_queue ON borrowers(is_reserved, priority, dpd);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_state ON calls(state);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_lease ON calls(lease_expires_at);")

            conn.execute("COMMIT;")

    # ---------------------------------------------------------
    # AGENT OPERATIONS WITH ATOMIC OCC (COMPARE-AND-SWAP)
    # ---------------------------------------------------------

    def upsert_agent(self, agent: Agent) -> None:
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            now_str = dt_to_iso(agent.updated_at)
            res_str = dt_to_iso(agent.reserved_at)
            conn.execute("""
                INSERT INTO agents (agent_id, name, state, version, team, skills, current_call_id, reserved_by, reserved_at, updated_at, total_calls_handled, total_talk_time_sec)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name = excluded.name,
                    state = excluded.state,
                    version = excluded.version,
                    team = excluded.team,
                    skills = excluded.skills,
                    current_call_id = excluded.current_call_id,
                    reserved_by = excluded.reserved_by,
                    reserved_at = excluded.reserved_at,
                    updated_at = excluded.updated_at,
                    total_calls_handled = excluded.total_calls_handled,
                    total_talk_time_sec = excluded.total_talk_time_sec;
            """, (
                agent.agent_id,
                agent.name,
                agent.state.value,
                agent.version,
                agent.team,
                agent.skills,
                agent.current_call_id,
                agent.reserved_by,
                res_str,
                now_str,
                agent.total_calls_handled,
                agent.total_talk_time_sec,
            ))
            conn.execute("COMMIT;")

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        with self.get_connection() as conn:
            cur = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
            row = cur.fetchone()
            if not row:
                return None
            return Agent(
                agent_id=row["agent_id"],
                name=row["name"],
                state=AgentState(row["state"]),
                version=row["version"],
                team=row["team"] if "team" in row.keys() else "T1",
                skills=row["skills"] if "skills" in row.keys() else "GENERAL",
                current_call_id=row["current_call_id"],
                reserved_by=row["reserved_by"],
                reserved_at=iso_to_dt(row["reserved_at"]),
                updated_at=iso_to_dt(row["updated_at"]) or utc_now(),
                total_calls_handled=row["total_calls_handled"],
                total_talk_time_sec=row["total_talk_time_sec"],
            )

    def list_available_agents(
        self,
        limit: int = 100,
        preferred_team: Optional[str] = None,
        preferred_skills: Optional[str] = None,
    ) -> List[Agent]:
        with self.get_connection() as conn:
            query = "SELECT * FROM agents WHERE state = ?"
            params: List[Any] = [AgentState.AVAILABLE.value]

            if preferred_team:
                query += " AND team = ?"
                params.append(preferred_team)
            if preferred_skills:
                query += " AND skills LIKE ?"
                params.append(f"%{preferred_skills}%")

            query += " ORDER BY updated_at ASC LIMIT ?"
            params.append(limit)

            cur = conn.execute(query, tuple(params))
            agents = []
            for row in cur.fetchall():
                agents.append(Agent(
                    agent_id=row["agent_id"],
                    name=row["name"],
                    state=AgentState(row["state"]),
                    version=row["version"],
                    team=row["team"] if "team" in row.keys() else "T1",
                    skills=row["skills"] if "skills" in row.keys() else "GENERAL",
                    current_call_id=row["current_call_id"],
                    reserved_by=row["reserved_by"],
                    reserved_at=iso_to_dt(row["reserved_at"]),
                    updated_at=iso_to_dt(row["updated_at"]) or utc_now(),
                    total_calls_handled=row["total_calls_handled"],
                    total_talk_time_sec=row["total_talk_time_sec"],
                ))

            # Fallback if preferred team had no available agents
            if not agents and (preferred_team or preferred_skills):
                return self.list_available_agents(limit=limit)

            return agents

    def list_all_agents(self, state_filter: Optional[str] = None, limit: int = 100) -> List[Agent]:
        with self.get_connection() as conn:
            if state_filter:
                cur = conn.execute(
                    "SELECT * FROM agents WHERE state = ? ORDER BY agent_id ASC LIMIT ?",
                    (state_filter.upper(), limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM agents ORDER BY agent_id ASC LIMIT ?", (limit,)
                )
            agents = []
            for row in cur.fetchall():
                agents.append(Agent(
                    agent_id=row["agent_id"],
                    name=row["name"],
                    state=AgentState(row["state"]),
                    version=row["version"],
                    team=row["team"] if "team" in row.keys() else "T1",
                    skills=row["skills"] if "skills" in row.keys() else "GENERAL",
                    current_call_id=row["current_call_id"],
                    reserved_by=row["reserved_by"],
                    reserved_at=iso_to_dt(row["reserved_at"]),
                    updated_at=iso_to_dt(row["updated_at"]) or utc_now(),
                    total_calls_handled=row["total_calls_handled"],
                    total_talk_time_sec=row["total_talk_time_sec"],
                ))
            return agents

    def count_agents_by_state(self) -> Dict[AgentState, int]:
        with self.get_connection() as conn:
            cur = conn.execute("SELECT state, COUNT(*) as cnt FROM agents GROUP BY state;")
            counts = {state: 0 for state in AgentState}
            for row in cur.fetchall():
                try:
                    counts[AgentState(row["state"])] = row["cnt"]
                except ValueError:
                    pass
            return counts

    def try_atomic_reserve_agent(
        self, agent_id: str, expected_version: int, worker_id: str
    ) -> bool:
        """Atomic Compare-And-Swap (CAS).

        Ensures that if two workers try to reserve the same agent at the exact same time,
        ONLY ONE succeeds (rowcount == 1). The other worker gets rowcount == 0 and fails safely.
        """
        now = utc_now()
        now_str = dt_to_iso(now)
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("""
                UPDATE agents
                SET state = ?,
                    reserved_by = ?,
                    reserved_at = ?,
                    version = version + 1,
                    updated_at = ?
                WHERE agent_id = ?
                  AND state = ?
                  AND version = ?;
            """, (
                AgentState.RESERVED.value,
                worker_id,
                now_str,
                now_str,
                agent_id,
                AgentState.AVAILABLE.value,
                expected_version,
            ))
            success = cur.rowcount == 1
            conn.execute("COMMIT;")
            return success

    def update_agent_state(
        self,
        agent_id: str,
        target_state: AgentState,
        current_call_id: Optional[str] = None,
        duration_to_add: float = 0.0,
    ) -> bool:
        """Updates agent state adhering to the AgentStateMachine lifecycle."""
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
            row = cur.fetchone()
            if not row:
                conn.execute("ROLLBACK;")
                return False

            current_state = AgentState(row["state"])
            if not AgentStateMachine.can_transition(current_state, target_state):
                conn.execute("COMMIT;")
                logger.warning(
                    f"Ignored invalid or out-of-order agent transition: {current_state.value} -> {target_state.value} for {agent_id}"
                )
                return False

            now_str = dt_to_iso(utc_now())
            calls_inc = 1 if target_state == AgentState.WRAP_UP else 0
            res_by = None if target_state in (AgentState.AVAILABLE, AgentState.OFFLINE) else row["reserved_by"]
            res_at = None if target_state in (AgentState.AVAILABLE, AgentState.OFFLINE) else row["reserved_at"]
            call_id = current_call_id if target_state in (AgentState.RESERVED, AgentState.DIALING, AgentState.CONNECTED) else None

            conn.execute("""
                UPDATE agents
                SET state = ?,
                    version = version + 1,
                    current_call_id = ?,
                    reserved_by = ?,
                    reserved_at = ?,
                    updated_at = ?,
                    total_calls_handled = total_calls_handled + ?,
                    total_talk_time_sec = total_talk_time_sec + ?
                WHERE agent_id = ?;
            """, (
                target_state.value,
                call_id,
                res_by,
                res_at,
                now_str,
                calls_inc,
                duration_to_add,
                agent_id,
            ))
            conn.execute("COMMIT;")
            return True

    # ---------------------------------------------------------
    # BORROWER OPERATIONS
    # ---------------------------------------------------------

    def upsert_borrower(self, borrower: Borrower) -> None:
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                INSERT INTO borrowers (borrower_id, account_id, name, phone, dpd, outstanding_amount, risk_segment, priority, call_attempts, last_call_at, is_reserved, reserved_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(borrower_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    name = excluded.name,
                    phone = excluded.phone,
                    dpd = excluded.dpd,
                    outstanding_amount = excluded.outstanding_amount,
                    risk_segment = excluded.risk_segment,
                    priority = excluded.priority,
                    call_attempts = excluded.call_attempts,
                    last_call_at = excluded.last_call_at,
                    is_reserved = excluded.is_reserved,
                    reserved_by = excluded.reserved_by;
            """, (
                borrower.borrower_id,
                borrower.account_id,
                borrower.name,
                borrower.phone,
                borrower.dpd,
                borrower.outstanding_amount,
                borrower.risk_segment,
                borrower.priority,
                borrower.call_attempts,
                dt_to_iso(borrower.last_call_at),
                1 if borrower.is_reserved else 0,
                borrower.reserved_by,
            ))
            conn.execute("COMMIT;")

    def get_borrower(self, borrower_id: str) -> Optional[Borrower]:
        with self.get_connection() as conn:
            cur = conn.execute("SELECT * FROM borrowers WHERE borrower_id = ?", (borrower_id,))
            row = cur.fetchone()
            if not row:
                return None
            return Borrower(
                borrower_id=row["borrower_id"],
                account_id=row["account_id"],
                name=row["name"],
                phone=row["phone"],
                dpd=row["dpd"],
                outstanding_amount=row["outstanding_amount"],
                risk_segment=row["risk_segment"],
                priority=row["priority"],
                call_attempts=row["call_attempts"],
                last_call_at=iso_to_dt(row["last_call_at"]),
                is_reserved=bool(row["is_reserved"]),
                reserved_by=row["reserved_by"],
            )

    def fetch_and_reserve_next_borrower(
        self, worker_id: str, max_attempts: int = 5
    ) -> Optional[Borrower]:
        """Atomically dequeues and reserves the highest priority queued borrower."""
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("""
                SELECT * FROM borrowers
                WHERE is_reserved = 0 AND call_attempts < ?
                ORDER BY priority ASC, dpd DESC, outstanding_amount DESC
                LIMIT 1;
            """, (max_attempts,))
            row = cur.fetchone()
            if not row:
                conn.execute("COMMIT;")
                return None

            borrower_id = row["borrower_id"]
            conn.execute("""
                UPDATE borrowers
                SET is_reserved = 1,
                    reserved_by = ?
                WHERE borrower_id = ?;
            """, (worker_id, borrower_id))
            conn.execute("COMMIT;")

            return Borrower(
                borrower_id=row["borrower_id"],
                account_id=row["account_id"],
                name=row["name"],
                phone=row["phone"],
                dpd=row["dpd"],
                outstanding_amount=row["outstanding_amount"],
                risk_segment=row["risk_segment"],
                priority=row["priority"],
                call_attempts=row["call_attempts"],
                last_call_at=iso_to_dt(row["last_call_at"]),
                is_reserved=True,
                reserved_by=worker_id,
            )

    def release_borrower(
        self, borrower_id: str, call_succeeded: bool = False
    ) -> None:
        """Releases borrower lock and updates attempt count."""
        now_str = dt_to_iso(utc_now())
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                UPDATE borrowers
                SET is_reserved = 0,
                    reserved_by = NULL,
                    call_attempts = call_attempts + 1,
                    last_call_at = ?
                WHERE borrower_id = ?;
            """, (now_str, borrower_id))
            conn.execute("COMMIT;")

    def count_available_borrowers(self, max_attempts: int = 5) -> int:
        with self.get_connection() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) as cnt FROM borrowers WHERE is_reserved = 0 AND call_attempts < ?",
                (max_attempts,),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0

    # ---------------------------------------------------------
    # CALL OPERATIONS & STATE PROGRESSION
    # ---------------------------------------------------------

    def create_call(self, call: Call) -> None:
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                INSERT INTO calls (
                    call_id, campaign_id, borrower_id, account_id, phone, agent_id,
                    state, mode, vendor_id, worker_id, lease_expires_at, created_at,
                    initiated_at, ringing_at, answered_at, connected_at, ended_at,
                    duration_sec, hangup_reason, retry_count, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                call.call_id,
                call.campaign_id,
                call.borrower_id,
                call.account_id,
                call.phone,
                call.agent_id,
                call.state.value,
                call.mode.value,
                call.vendor_id,
                call.worker_id,
                dt_to_iso(call.lease_expires_at),
                dt_to_iso(call.created_at),
                dt_to_iso(call.initiated_at),
                dt_to_iso(call.ringing_at),
                dt_to_iso(call.answered_at),
                dt_to_iso(call.connected_at),
                dt_to_iso(call.ended_at),
                call.duration_sec,
                call.hangup_reason,
                call.retry_count,
                call.version,
            ))
            conn.execute("COMMIT;")

    def get_call(self, call_id: str) -> Optional[Call]:
        with self.get_connection() as conn:
            cur = conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,))
            row = cur.fetchone()
            if not row:
                return None
            return Call(
                call_id=row["call_id"],
                campaign_id=row["campaign_id"],
                borrower_id=row["borrower_id"],
                account_id=row["account_id"],
                phone=row["phone"],
                agent_id=row["agent_id"],
                state=CallState(row["state"]),
                mode=PacingMode(row["mode"]),
                vendor_id=row["vendor_id"],
                worker_id=row["worker_id"],
                lease_expires_at=iso_to_dt(row["lease_expires_at"]),
                created_at=iso_to_dt(row["created_at"]) or utc_now(),
                initiated_at=iso_to_dt(row["initiated_at"]),
                ringing_at=iso_to_dt(row["ringing_at"]),
                answered_at=iso_to_dt(row["answered_at"]),
                connected_at=iso_to_dt(row["connected_at"]),
                ended_at=iso_to_dt(row["ended_at"]),
                duration_sec=row["duration_sec"],
                hangup_reason=row["hangup_reason"],
                retry_count=row["retry_count"],
                version=row["version"],
            )

    def transition_call(
        self,
        call_id: str,
        target_state: CallState,
        agent_id: Optional[str] = None,
        duration_sec: Optional[float] = None,
        hangup_reason: Optional[str] = None,
        lease_seconds: int = 30,
    ) -> Tuple[bool, str]:
        """Atomically transitions call state using monotonic rules.

        Returns (success, outcome_reason).
        """
        now = utc_now()
        now_str = dt_to_iso(now)
        lease_str = dt_to_iso(now + timedelta(seconds=lease_seconds))

        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,))
            row = cur.fetchone()
            if not row:
                conn.execute("ROLLBACK;")
                return False, "CALL_NOT_FOUND"

            current_state = CallState(row["state"])
            should_apply, reason_code = CallStateMachine.evaluate_transition(
                current_state, target_state
            )

            if not should_apply:
                conn.execute("COMMIT;")
                return False, reason_code  # DUPLICATE or OUT_OF_ORDER safely handled

            # Dynamic timestamps
            init_at = row["initiated_at"] or (now_str if target_state == CallState.INITIATED else None)
            ring_at = row["ringing_at"] or (now_str if target_state == CallState.RINGING else None)
            ans_at = row["answered_at"] or (now_str if target_state == CallState.ANSWERED else None)
            conn_at = row["connected_at"] or (now_str if target_state == CallState.CONNECTED else None)
            end_at = row["ended_at"] or (now_str if target_state in (CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED) else None)

            assigned_agent = agent_id or row["agent_id"]
            dur = duration_sec if duration_sec is not None else row["duration_sec"]
            h_reason = hangup_reason or row["hangup_reason"]

            conn.execute("""
                UPDATE calls
                SET state = ?,
                    agent_id = ?,
                    initiated_at = COALESCE(initiated_at, ?),
                    ringing_at = COALESCE(ringing_at, ?),
                    answered_at = COALESCE(answered_at, ?),
                    connected_at = COALESCE(connected_at, ?),
                    ended_at = COALESCE(ended_at, ?),
                    duration_sec = ?,
                    hangup_reason = ?,
                    lease_expires_at = ?,
                    version = version + 1
                WHERE call_id = ?;
            """, (
                target_state.value,
                assigned_agent,
                init_at,
                ring_at,
                ans_at,
                conn_at,
                end_at,
                dur,
                h_reason,
                lease_str,
                call_id,
            ))
            conn.execute("COMMIT;")
            return True, "APPLIED"

    def count_calls_by_state(self) -> Dict[CallState, int]:
        with self.get_connection() as conn:
            cur = conn.execute("SELECT state, COUNT(*) as cnt FROM calls GROUP BY state;")
            counts = {state: 0 for state in CallState}
            for row in cur.fetchall():
                try:
                    counts[CallState(row["state"])] = row["cnt"]
                except ValueError:
                    pass
            return counts

    def get_expired_leased_calls(self) -> List[Call]:
        """Finds active non-terminal calls whose worker lease has expired."""
        now_str = dt_to_iso(utc_now())
        with self.get_connection() as conn:
            cur = conn.execute("""
                SELECT * FROM calls
                WHERE lease_expires_at < ?
                  AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED');
            """, (now_str,))
            calls = []
            for row in cur.fetchall():
                calls.append(Call(
                    call_id=row["call_id"],
                    campaign_id=row["campaign_id"],
                    borrower_id=row["borrower_id"],
                    account_id=row["account_id"],
                    phone=row["phone"],
                    agent_id=row["agent_id"],
                    state=CallState(row["state"]),
                    mode=PacingMode(row["mode"]),
                    vendor_id=row["vendor_id"],
                    worker_id=row["worker_id"],
                    lease_expires_at=iso_to_dt(row["lease_expires_at"]),
                    created_at=iso_to_dt(row["created_at"]) or utc_now(),
                    initiated_at=iso_to_dt(row["initiated_at"]),
                    ringing_at=iso_to_dt(row["ringing_at"]),
                    answered_at=iso_to_dt(row["answered_at"]),
                    connected_at=iso_to_dt(row["connected_at"]),
                    ended_at=iso_to_dt(row["ended_at"]),
                    duration_sec=row["duration_sec"],
                    hangup_reason=row["hangup_reason"],
                    retry_count=row["retry_count"],
                    version=row["version"],
                ))
            return calls

    # ---------------------------------------------------------
    # TELEPHONY EVENTS AUDIT & IDEMPOTENCY
    # ---------------------------------------------------------

    def record_telephony_event(self, event: TelephonyEvent) -> bool:
        """Records an incoming webhook event.

        Returns False if the event was already recorded (idempotency key collision).
        """
        payload_json = json.dumps(event.raw_payload)
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute("""
                    INSERT INTO telephony_events (
                        event_id, call_id, event_type, timestamp, vendor_id, raw_payload, idempotency_key, processed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    event.event_id,
                    event.call_id,
                    event.event_type,
                    dt_to_iso(event.timestamp),
                    event.vendor_id,
                    payload_json,
                    event.idempotency_key,
                    1 if event.processed else 0,
                ))
                conn.execute("COMMIT;")
                return True
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK;")
                return False  # Duplicate event

    # ---------------------------------------------------------
    # SAFETY CONTROLLER AUDIT LOGS
    # ---------------------------------------------------------

    def record_safety_evaluation(self, eval_res: SafetyEvaluation) -> None:
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                INSERT INTO safety_logs (
                    timestamp, action, requested_calls, authorized_calls, reason, active_mode, metrics_snapshot
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (
                dt_to_iso(eval_res.timestamp),
                eval_res.action.value,
                eval_res.requested_calls,
                eval_res.authorized_calls,
                eval_res.reason,
                eval_res.active_mode.value,
                json.dumps(eval_res.metrics_snapshot),
            ))
            conn.execute("COMMIT;")

    def get_recent_safety_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM safety_logs ORDER BY id DESC LIMIT ?", (limit,)
            )
            logs = []
            for row in cur.fetchall():
                logs.append({
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "action": row["action"],
                    "requested_calls": row["requested_calls"],
                    "authorized_calls": row["authorized_calls"],
                    "reason": row["reason"],
                    "active_mode": row["active_mode"],
                    "metrics": json.loads(row["metrics_snapshot"]),
                })
            return logs

    # ---------------------------------------------------------
    # METRICS & ANALYTICS
    # ---------------------------------------------------------

    def get_operational_metrics(self) -> Dict[str, Any]:
        """Computes live pacing and compliance statistics."""
        with self.get_connection() as conn:
            # Calls counts
            call_counts = self.count_calls_by_state()
            agent_counts = self.count_agents_by_state()

            # Completed vs Abandoned
            cur = conn.execute("""
                SELECT 
                    COUNT(*) as total_outcomes,
                    SUM(CASE WHEN hangup_reason = 'COMPLIANCE_ABANDONED' THEN 1 ELSE 0 END) as abandoned,
                    SUM(CASE WHEN state = 'COMPLETED' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN state = 'CONNECTED' THEN 1 ELSE 0 END) as connected,
                    AVG(CASE WHEN duration_sec > 0 THEN duration_sec ELSE NULL END) as avg_duration
                FROM calls;
            """)
            outcomes = cur.fetchone()
            total_outcomes = outcomes["total_outcomes"] or 0
            abandoned = outcomes["abandoned"] or 0
            abandon_rate = (abandoned / total_outcomes) if total_outcomes > 0 else 0.0

            total_agents = sum(agent_counts.values())
            busy_agents = agent_counts[AgentState.CONNECTED] + agent_counts[AgentState.DIALING] + agent_counts[AgentState.WRAP_UP]
            utilization = (busy_agents / total_agents) if total_agents > 0 else 0.0

            return {
                "agents": {k.value: v for k, v in agent_counts.items()},
                "calls": {k.value: v for k, v in call_counts.items()},
                "total_agents": total_agents,
                "available_agents": agent_counts[AgentState.AVAILABLE],
                "busy_agents": busy_agents,
                "agent_utilization": utilization,
                "ringing_calls": call_counts[CallState.RINGING],
                "connected_calls": call_counts[CallState.CONNECTED],
                "abandoned_calls": abandoned,
                "abandon_rate": abandon_rate,
                "avg_talk_time_sec": outcomes["avg_duration"] or 0.0,
            }
