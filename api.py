"""FastAPI REST API & Webhook Ingestion Engine for SmartDialer.

Provides production-grade endpoints for external telephony webhooks (Plivo / Twilio),
campaign lifecycle management, live compliance metrics, and safety audit logs.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Compatibility shim for Starlette 1.9+ and FastAPI Router
import starlette.routing
_orig_router_init = starlette.routing.Router.__init__
def _compat_router_init(self, *args, **kwargs):
    kwargs.pop("on_startup", None)
    kwargs.pop("on_shutdown", None)
    return _orig_router_init(self, *args, **kwargs)
starlette.routing.Router.__init__ = _compat_router_init

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
import uvicorn

from smartdialer.campaign import CampaignEngine
from smartdialer.dataset import DatasetLoader
from smartdialer.db import Database, utc_now
from smartdialer.models import AgentState, PacingMode, TelephonyEvent
from smartdialer.reaper import ReconciliationReaper
from smartdialer.telecom import PlivoProvider, ProviderA, ProviderB


app = FastAPI(
    title="SmartDialer Collections API",
    description="High-concurrency intelligent outbound dialing API with deterministic safety controller boundaries.",
    version="1.0.0",
)

# Global in-memory state
db = Database(":memory:")
loader = DatasetLoader()
loader.load_agents(db, limit=50)
loader.load_borrowers(db, limit=500)
provider = ProviderA(answer_rate=0.20, avg_talk_time_sec=120.0)
engine = CampaignEngine(
    db=db,
    campaign_id="CAMP_REST_API",
    provider=provider,
    mode=PacingMode.PROGRESSIVE,
)
reaper = ReconciliationReaper(db)


class CreateAgentRequest(BaseModel):
    agent_id: str
    name: str
    team: Optional[str] = "T1"
    skills: Optional[str] = "GENERAL"
    initial_state: Optional[AgentState] = AgentState.AVAILABLE


class UpdateAgentStatusRequest(BaseModel):
    state: AgentState


class TickRequest(BaseModel):
    mode: Optional[PacingMode] = PacingMode.PROGRESSIVE
    campaign_id: Optional[str] = "CAMP_REST_API"


class WebhookPayload(BaseModel):
    call_id: str
    event_type: str  # INITIATED, RINGING, ANSWERED, COMPLETED, FAILED
    vendor_id: Optional[str] = "CUSTOM_GATEWAY"
    idempotency_key: Optional[str] = None
    duration_sec: Optional[float] = None
    hangup_reason: Optional[str] = None


@app.get("/")
def root():
    return {
        "service": "SmartDialer API",
        "status": "ONLINE",
        "timestamp": utc_now().isoformat(),
        "docs_url": "/docs",
    }


@app.get("/api/metrics")
def get_metrics():
    """Returns live agent state, active call counts, and statutory compliance abandon rates."""
    return db.get_operational_metrics()


@app.get("/api/agents")
def list_agents(state: Optional[str] = None, limit: int = Query(50, ge=1, le=200)):
    """Lists agents with optional state filtering (AVAILABLE, CONNECTED, WRAP_UP, etc.)."""
    agents = db.list_all_agents(state_filter=state, limit=limit)
    return [
        {
            "agent_id": a.agent_id,
            "name": a.name,
            "state": a.state.value,
            "team": a.team,
            "skills": a.skills,
            "version": a.version,
            "current_call_id": a.current_call_id,
            "calls_handled": a.total_calls_handled,
        }
        for a in agents
    ]


@app.post("/api/agents")
def create_agent(payload: CreateAgentRequest):
    """Dynamically adds a new agent with team and skills to the dialer pool."""
    existing = db.get_agent(payload.agent_id)
    if existing:
        raise HTTPException(status_code=400, detail=f"Agent '{payload.agent_id}' already exists.")

    new_agent = Agent(
        agent_id=payload.agent_id,
        name=payload.name,
        team=payload.team or "T1",
        skills=payload.skills or "GENERAL",
        state=payload.initial_state or AgentState.AVAILABLE,
    )
    db.upsert_agent(new_agent)
    return {"status": "CREATED", "agent_id": new_agent.agent_id, "state": new_agent.state.value}


@app.put("/api/agents/{agent_id}/status")
def update_agent_status(agent_id: str, payload: UpdateAgentStatusRequest):
    """Updates an agent's lifecycle status (e.g. set to PAUSED, AVAILABLE, or OFFLINE)."""
    agent = db.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    success = db.update_agent_state(agent_id, payload.state)
    if not success:
        raise HTTPException(status_code=400, detail=f"Invalid transition from {agent.state.value} to {payload.state.value}.")

    return {"status": "UPDATED", "agent_id": agent_id, "new_state": payload.state.value}


@app.post("/api/campaign/tick")
def trigger_campaign_tick(req: TickRequest):
    """Executes a single dialing evaluation tick through the Safety Controller pipeline."""
    engine.mode = req.mode or PacingMode.PROGRESSIVE
    result = engine.execute_tick()
    return {
        "timestamp": result.timestamp.isoformat(),
        "mode": result.mode.value,
        "recommendation": {
            "requested_calls": result.recommendation.requested_calls,
            "explanation": result.recommendation.explanation,
        },
        "safety_evaluation": {
            "action": result.safety_eval.action.value,
            "authorized_calls": result.safety_eval.authorized_calls,
            "reason": result.safety_eval.reason,
        },
        "dispatched_calls_count": len(result.dispatched_calls),
    }


@app.get("/api/safety/logs")
def get_safety_logs(limit: int = Query(25, ge=1, le=100)):
    """Retrieves immutable safety boundary decision logs."""
    return db.get_recent_safety_logs(limit=limit)


@app.post("/api/reaper/reconcile")
def run_reaper():
    """Scans for and reconciles expired worker leases and orphaned calls."""
    recov_c, recov_a = reaper.reconcile_stale_leases()
    return {
        "status": "SUCCESS",
        "recovered_calls": recov_c,
        "recovered_agents": recov_a,
        "timestamp": utc_now().isoformat(),
    }


@app.post("/webhooks/telecom")
def generic_telecom_webhook(payload: WebhookPayload):
    """Idempotent webhook endpoint for external telecom providers."""
    key = payload.idempotency_key or f"{payload.call_id}_{payload.event_type}"
    event = TelephonyEvent(
        event_id=f"EVT_{datetime.now(timezone.utc).timestamp()}",
        call_id=payload.call_id,
        event_type=payload.event_type,
        timestamp=utc_now(),
        vendor_id=payload.vendor_id or "GENERIC",
        raw_payload={"duration": payload.duration_sec, "reason": payload.hangup_reason},
        idempotency_key=key,
    )
    engine.allocator.process_telephony_event(event)
    return {"status": "RECEIVED", "call_id": payload.call_id, "event": payload.event_type}


@app.post("/webhooks/plivo")
async def plivo_webhook(request: Request, call_id: Optional[str] = None, event: Optional[str] = None):
    """Webhook callback handler for live Plivo telephony events."""
    form_data = await request.form()
    event_type = event or form_data.get("Event", "ANSWERED")
    c_id = call_id or form_data.get("CallUUID", "UNKNOWN")

    telephony_event = TelephonyEvent(
        event_id=f"PLIVO_{datetime.now(timezone.utc).timestamp()}",
        call_id=c_id,
        event_type=event_type,
        timestamp=utc_now(),
        vendor_id="PLIVO_TELECOM",
        raw_payload=dict(form_data),
        idempotency_key=f"PLIVO_{c_id}_{event_type}",
    )
    engine.allocator.process_telephony_event(telephony_event)
    return {"status": "OK", "call_id": c_id, "event": event_type}


if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
