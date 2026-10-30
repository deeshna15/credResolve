"""Domain models and lifecycle states for SmartDialer.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


class CallState(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Monotonic progression ranks to guard against out-of-order telecom events.
# Terminal states (COMPLETED, FAILED, CANCELLED) share rank 100 to prevent any
# backward mutation from delayed in-flight webhooks.
CALL_STATE_RANKS: Dict[CallState, int] = {
    CallState.QUEUED: 0,
    CallState.RESERVED: 10,
    CallState.INITIATED: 20,
    CallState.RINGING: 30,
    CallState.ANSWERED: 40,
    CallState.CONNECTED: 50,
    CallState.COMPLETED: 100,
    CallState.FAILED: 100,
    CallState.CANCELLED: 100,
}

TERMINAL_CALL_STATES = {CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}


class PacingMode(str, Enum):
    PROGRESSIVE = "PROGRESSIVE"
    PREDICTIVE = "PREDICTIVE"


class SafetyAction(str, Enum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    FALLBACK_TO_PROGRESSIVE = "FALLBACK_TO_PROGRESSIVE"


@dataclass
class Agent:
    agent_id: str
    name: str
    state: AgentState = AgentState.AVAILABLE
    version: int = 1
    team: str = "T1"                 # T1 (Early Stage), T2 (Mid Stage), T3 (Senior Recovery)
    skills: str = "GENERAL"          # e.g., HINDI, ENGLISH, HIGH_VALUE, LEGAL
    current_call_id: Optional[str] = None
    reserved_by: Optional[str] = None  # worker ID holding reservation
    reserved_at: Optional[datetime] = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total_calls_handled: int = 0
    total_talk_time_sec: float = 0.0

    def is_available(self) -> bool:
        return self.state == AgentState.AVAILABLE


@dataclass
class Borrower:
    borrower_id: str
    account_id: str
    name: str
    phone: str
    dpd: int = 0
    outstanding_amount: float = 0.0
    risk_segment: str = "MEDIUM"
    priority: int = 1  # 1 = highest priority
    call_attempts: int = 0
    last_call_at: Optional[datetime] = None
    is_reserved: bool = False
    reserved_by: Optional[str] = None


@dataclass
class Call:
    call_id: str
    campaign_id: str
    borrower_id: str
    account_id: str
    phone: str
    agent_id: Optional[str] = None
    state: CallState = CallState.QUEUED
    mode: PacingMode = PacingMode.PROGRESSIVE
    vendor_id: Optional[str] = None
    worker_id: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    initiated_at: Optional[datetime] = None
    ringing_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None
    connected_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_sec: float = 0.0
    hangup_reason: Optional[str] = None
    retry_count: int = 0
    version: int = 1

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_CALL_STATES


@dataclass
class TelephonyEvent:
    event_id: str
    call_id: str
    event_type: str  # INITIATED, RINGING, ANSWERED, COMPLETED, FAILED
    timestamp: datetime
    vendor_id: str
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    idempotency_key: Optional[str] = None
    processed: bool = False


@dataclass
class PacingRecommendation:
    timestamp: datetime
    mode: PacingMode
    requested_calls: int
    formula_inputs: Dict[str, Any]
    explanation: str


@dataclass
class SafetyEvaluation:
    timestamp: datetime
    action: SafetyAction
    requested_calls: int
    authorized_calls: int
    reason: str
    active_mode: PacingMode
    metrics_snapshot: Dict[str, Any]
