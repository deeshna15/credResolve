"""Telecom Provider abstractions, mock implementations (Provider A and Provider B),
real Plivo integration, and circuit breaker health monitors.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
import enum
import logging
import os
import random
import time
from typing import Any, Callable, Dict, List, Optional
import uuid

from smartdialer.models import CallState, TelephonyEvent

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CircuitState(str, enum.Enum):
    CLOSED = "CLOSED"        # Normal operation
    OPEN = "OPEN"            # Provider degraded / tripping calls
    HALF_OPEN = "HALF_OPEN"  # Testing recovery


@dataclass
class ProviderHealth:
    vendor_id: str
    status: str
    circuit_state: CircuitState
    total_calls: int = 0
    failed_calls: int = 0
    consecutive_timeouts: int = 0
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0


class TelecomProvider(ABC):
    """Abstract interface defining the telecom provider contract."""

    def __init__(
        self,
        vendor_id: str,
        answer_rate: float = 0.20,
        avg_talk_time_sec: float = 120.0,
        avg_ring_time_sec: float = 15.0,
    ):
        self.vendor_id = vendor_id
        self.answer_rate = answer_rate
        self.avg_talk_time_sec = avg_talk_time_sec
        self.avg_ring_time_sec = avg_ring_time_sec
        self.total_initiated = 0
        self.total_failed = 0
        self.total_timeouts = 0
        self.consecutive_errors = 0

    @abstractmethod
    def initiate_call(
        self,
        call_id: str,
        phone: str,
        event_callback: Callable[[TelephonyEvent], None],
    ) -> bool:
        """Initiates an outbound call and emits events back asynchronously via callback."""
        pass

    @abstractmethod
    def hangup(self, call_id: str) -> bool:
        """Terminates an active call."""
        pass

    def get_health(self) -> ProviderHealth:
        err_rate = (self.total_failed / self.total_initiated) if self.total_initiated > 0 else 0.0
        circuit = CircuitState.OPEN if self.consecutive_errors >= 4 or err_rate > 0.40 else CircuitState.CLOSED
        return ProviderHealth(
            vendor_id=self.vendor_id,
            status="DEGRADED" if circuit == CircuitState.OPEN else "HEALTHY",
            circuit_state=circuit,
            total_calls=self.total_initiated,
            failed_calls=self.total_failed,
            consecutive_timeouts=self.consecutive_errors,
            error_rate=err_rate,
        )


class ProviderA(TelecomProvider):
    """Provider A: Fast, reliable, low failure rate, perfect event ordering."""

    def __init__(
        self,
        vendor_id: str = "PROVIDER_A",
        answer_rate: float = 0.20,
        avg_talk_time_sec: float = 120.0,
        failure_rate: float = 0.01,
        auto_complete: bool = True,
    ):
        super().__init__(vendor_id, answer_rate, avg_talk_time_sec)
        self.failure_rate = failure_rate
        self.auto_complete = auto_complete

    def initiate_call(
        self,
        call_id: str,
        phone: str,
        event_callback: Callable[[TelephonyEvent], None],
    ) -> bool:
        self.total_initiated += 1

        # Emit INITIATED
        event_callback(TelephonyEvent(
            event_id=f"EVT_{uuid.uuid4().hex[:10]}",
            call_id=call_id,
            event_type="INITIATED",
            timestamp=utc_now(),
            vendor_id=self.vendor_id,
            idempotency_key=f"{call_id}_INITIATED",
        ))

        if not self.auto_complete:
            # Leave call in-flight (for crash and concurrency testing)
            return True

        # Check for instant failure
        if random.random() < self.failure_rate:
            self.total_failed += 1
            self.consecutive_errors += 1
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="FAILED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                raw_payload={"reason": "NETWORK_FAILURE"},
                idempotency_key=f"{call_id}_FAILED",
            ))
            return False

        self.consecutive_errors = 0

        # Emit RINGING
        event_callback(TelephonyEvent(
            event_id=f"EVT_{uuid.uuid4().hex[:10]}",
            call_id=call_id,
            event_type="RINGING",
            timestamp=utc_now(),
            vendor_id=self.vendor_id,
            idempotency_key=f"{call_id}_RINGING",
        ))

        # Determine Answer vs No-Answer
        answers = random.random() < self.answer_rate
        if answers:
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="ANSWERED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                idempotency_key=f"{call_id}_ANSWERED",
            ))
        else:
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="FAILED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                raw_payload={"reason": "NO_ANSWER_OR_BUSY"},
                idempotency_key=f"{call_id}_FAILED",
            ))

        return True

    def hangup(self, call_id: str) -> bool:
        return True


class ProviderB(TelecomProvider):
    """Provider B: Real-world chaotic telephony.

    Characteristics:
    - Slower response
    - Occasional timeouts
    - Injects duplicate events (e.g., duplicate ANSWERED)
    - Injects out-of-order events (e.g., COMPLETED before ANSWERED)
    """

    def __init__(
        self,
        vendor_id: str = "PROVIDER_B",
        answer_rate: float = 0.20,
        avg_talk_time_sec: float = 120.0,
        timeout_rate: float = 0.10,
        duplicate_event_rate: float = 0.20,
        out_of_order_rate: float = 0.15,
    ):
        super().__init__(vendor_id, answer_rate, avg_talk_time_sec)
        self.timeout_rate = timeout_rate
        self.duplicate_event_rate = duplicate_event_rate
        self.out_of_order_rate = out_of_order_rate

    def initiate_call(
        self,
        call_id: str,
        phone: str,
        event_callback: Callable[[TelephonyEvent], None],
    ) -> bool:
        self.total_initiated += 1

        # Check for simulated timeout
        if random.random() < self.timeout_rate:
            self.total_timeouts += 1
            self.total_failed += 1
            self.consecutive_errors += 1
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="FAILED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                raw_payload={"reason": "PROVIDER_GATEWAY_TIMEOUT"},
                idempotency_key=f"{call_id}_TIMEOUT",
            ))
            return False

        self.consecutive_errors = 0

        # Normal event initiation
        event_callback(TelephonyEvent(
            event_id=f"EVT_{uuid.uuid4().hex[:10]}",
            call_id=call_id,
            event_type="INITIATED",
            timestamp=utc_now(),
            vendor_id=self.vendor_id,
            idempotency_key=f"{call_id}_INITIATED",
        ))

        # Check if events arrive OUT OF ORDER
        answers = random.random() < self.answer_rate
        is_out_of_order = random.random() < self.out_of_order_rate

        if is_out_of_order and answers:
            # Anomaly: Provider sends COMPLETED first, then ANSWERED, then RINGING
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="COMPLETED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                raw_payload={"reason": "OUT_OF_ORDER_SIMULATION", "duration": 45.0},
                idempotency_key=f"{call_id}_COMPLETED",
            ))
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="ANSWERED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                idempotency_key=f"{call_id}_ANSWERED",
            ))
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="RINGING",
                timestamp=utc_now(),
                idempotency_key=f"{call_id}_RINGING",
                vendor_id=self.vendor_id,
            ))
            return True

        # Standard orderly delivery
        event_callback(TelephonyEvent(
            event_id=f"EVT_{uuid.uuid4().hex[:10]}",
            call_id=call_id,
            event_type="RINGING",
            timestamp=utc_now(),
            vendor_id=self.vendor_id,
            idempotency_key=f"{call_id}_RINGING",
        ))

        if answers:
            # Emit ANSWERED
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="ANSWERED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                idempotency_key=f"{call_id}_ANSWERED",
            ))

            # Anomaly: Injects DUPLICATE events
            if random.random() < self.duplicate_event_rate:
                for dup_idx in range(2):
                    event_callback(TelephonyEvent(
                        event_id=f"EVT_{uuid.uuid4().hex[:10]}_DUP{dup_idx}",
                        call_id=call_id,
                        event_type="ANSWERED",
                        timestamp=utc_now(),
                        vendor_id=self.vendor_id,
                        idempotency_key=f"{call_id}_ANSWERED",
                    ))
        else:
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="FAILED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                raw_payload={"reason": "NO_ANSWER_OR_BUSY"},
                idempotency_key=f"{call_id}_FAILED",
            ))

        return True

    def hangup(self, call_id: str) -> bool:
        return True


class PlivoProvider(TelecomProvider):
    """Real Plivo Telephony Integration ('Cherry on the Cake').

    Uses Plivo's REST API for outbound dialing when PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN
    are configured. Falls back seamlessly to simulated live calling if credentials are unset.
    """

    def __init__(
        self,
        auth_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
        callback_url: str = "http://localhost:8000/webhooks/plivo",
        answer_rate: float = 0.20,
        avg_talk_time_sec: float = 120.0,
    ):
        super().__init__("PLIVO_TELECOM", answer_rate, avg_talk_time_sec)
        self.auth_id = auth_id or os.getenv("PLIVO_AUTH_ID")
        self.auth_token = auth_token or os.getenv("PLIVO_AUTH_TOKEN")
        self.from_number = from_number or os.getenv("PLIVO_FROM_NUMBER", "+15551234567")
        self.callback_url = callback_url
        self.is_live = bool(self.auth_id and self.auth_token)

        if self.is_live:
            logger.info("PlivoProvider: Initialized in LIVE mode using configured credentials.")
        else:
            logger.info("PlivoProvider: No Plivo credentials found. Running in MOCK simulation mode.")

    def initiate_call(
        self,
        call_id: str,
        phone: str,
        event_callback: Callable[[TelephonyEvent], None],
    ) -> bool:
        self.total_initiated += 1

        if self.is_live:
            import requests
            url = f"https://api.plivo.com/v1/Account/{self.auth_id}/Call/"
            payload = {
                "from": self.from_number,
                "to": phone,
                "answer_url": f"{self.callback_url}?call_id={call_id}&event=ANSWERED",
                "answer_method": "POST",
                "hangup_url": f"{self.callback_url}?call_id={call_id}&event=COMPLETED",
                "hangup_method": "POST",
            }
            try:
                resp = requests.post(url, auth=(self.auth_id, self.auth_token), json=payload, timeout=5.0)
                if resp.status_code in (200, 201, 202):
                    self.consecutive_errors = 0
                    event_callback(TelephonyEvent(
                        event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                        call_id=call_id,
                        event_type="INITIATED",
                        timestamp=utc_now(),
                        vendor_id=self.vendor_id,
                        raw_payload=resp.json(),
                        idempotency_key=f"{call_id}_INITIATED",
                    ))
                    return True
                else:
                    self.total_failed += 1
                    self.consecutive_errors += 1
                    event_callback(TelephonyEvent(
                        event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                        call_id=call_id,
                        event_type="FAILED",
                        timestamp=utc_now(),
                        vendor_id=self.vendor_id,
                        raw_payload={"status_code": resp.status_code, "text": resp.text},
                        idempotency_key=f"{call_id}_FAILED",
                    ))
                    return False
            except Exception as e:
                self.total_failed += 1
                self.consecutive_errors += 1
                event_callback(TelephonyEvent(
                    event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                    call_id=call_id,
                    event_type="FAILED",
                    timestamp=utc_now(),
                    vendor_id=self.vendor_id,
                    raw_payload={"error": str(e)},
                    idempotency_key=f"{call_id}_FAILED",
                ))
                return False

        # Mock fallback mode
        event_callback(TelephonyEvent(
            event_id=f"EVT_{uuid.uuid4().hex[:10]}",
            call_id=call_id,
            event_type="INITIATED",
            timestamp=utc_now(),
            vendor_id=self.vendor_id,
            idempotency_key=f"{call_id}_INITIATED",
        ))
        event_callback(TelephonyEvent(
            event_id=f"EVT_{uuid.uuid4().hex[:10]}",
            call_id=call_id,
            event_type="RINGING",
            timestamp=utc_now(),
            vendor_id=self.vendor_id,
            idempotency_key=f"{call_id}_RINGING",
        ))
        if random.random() < self.answer_rate:
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="ANSWERED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                idempotency_key=f"{call_id}_ANSWERED",
            ))
        else:
            event_callback(TelephonyEvent(
                event_id=f"EVT_{uuid.uuid4().hex[:10]}",
                call_id=call_id,
                event_type="FAILED",
                timestamp=utc_now(),
                vendor_id=self.vendor_id,
                raw_payload={"reason": "NO_ANSWER"},
                idempotency_key=f"{call_id}_FAILED",
            ))
        return True

    def hangup(self, call_id: str) -> bool:
        if self.is_live and self.auth_id and self.auth_token:
            import requests
            url = f"https://api.plivo.com/v1/Account/{self.auth_id}/Call/{call_id}/"
            try:
                requests.delete(url, auth=(self.auth_id, self.auth_token), timeout=3.0)
            except Exception:
                pass
        return True


class CircuitBreaker:
    """Monitors provider failure rates and trips open to protect system from cascade failures."""

    def __init__(
        self,
        failure_threshold: float = 0.25,
        consecutive_timeout_limit: int = 3,
        recovery_time_sec: float = 10.0,
    ):
        self.failure_threshold = failure_threshold
        self.consecutive_timeout_limit = consecutive_timeout_limit
        self.recovery_time_sec = recovery_time_sec
        self.state = CircuitState.CLOSED
        self.last_trip_time: Optional[datetime] = None

    def should_allow_call(self, provider_health: ProviderHealth) -> bool:
        if self.state == CircuitState.OPEN:
            if self.last_trip_time:
                elapsed = (utc_now() - self.last_trip_time).total_seconds()
                if elapsed > self.recovery_time_sec:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker moved to HALF_OPEN to probe provider.")
                    return True
            return False

        if (
            provider_health.consecutive_timeouts >= self.consecutive_timeout_limit
            or provider_health.error_rate > self.failure_threshold
        ):
            self.state = CircuitState.OPEN
            self.last_trip_time = utc_now()
            logger.warning(
                f"Circuit breaker tripped OPEN for {provider_health.vendor_id}. "
                f"Error rate: {provider_health.error_rate:.1%}, Timeouts: {provider_health.consecutive_timeouts}"
            )
            return False

        return True
