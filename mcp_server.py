"""SmartDialer Model Context Protocol (MCP) Server.

Exposes native tools for AI agents (Antigravity / Claude / Cursor) to inspect dialer metrics,
execute progressive/predictive pacing ticks, audit safety decisions, and simulate failure recoveries.
"""

from __future__ import annotations
import json
from typing import Any, Dict, List, Optional
from mcp.server.mcpserver import MCPServer

from smartdialer.campaign import CampaignEngine
from smartdialer.dataset import DatasetLoader
from smartdialer.db import Database
from smartdialer.models import AgentState, PacingMode
from smartdialer.reaper import ReconciliationReaper
from smartdialer.telecom import ProviderA

# Initialize MCPServer instance
server = MCPServer("smartdialer-mcp")

# Initialize shared campaign resources
db = Database(":memory:")
loader = DatasetLoader()
loader.load_agents(db, limit=50)
loader.load_borrowers(db, limit=500)
provider = ProviderA(answer_rate=0.20, avg_talk_time_sec=120.0)
engine = CampaignEngine(
    db=db,
    campaign_id="CAMP_MCP",
    provider=provider,
    mode=PacingMode.PROGRESSIVE,
)
reaper = ReconciliationReaper(db)


@server.tool()
def get_dialer_metrics() -> str:
    """Returns live collections dialer metrics: agent utilization, ringing calls, and compliance abandon rate."""
    metrics = db.get_operational_metrics()
    return json.dumps(metrics, indent=2)


@server.tool()
def execute_pacing_tick(mode: str = "PROGRESSIVE") -> str:
    """Executes a single dialing cycle through the Safety Controller pipeline.

    Args:
        mode: Either 'PROGRESSIVE' or 'PREDICTIVE'.
    """
    engine.mode = PacingMode.PREDICTIVE if mode.upper() == "PREDICTIVE" else PacingMode.PROGRESSIVE
    result = engine.execute_tick()
    response = {
        "timestamp": result.timestamp.isoformat(),
        "mode": result.mode.value,
        "requested_calls": result.recommendation.requested_calls,
        "safety_action": result.safety_eval.action.value,
        "authorized_calls": result.safety_eval.authorized_calls,
        "safety_reason": result.safety_eval.reason,
        "dispatched_count": len(result.dispatched_calls),
    }
    return json.dumps(response, indent=2)


@server.tool()
def inspect_safety_logs(limit: int = 10) -> str:
    """Retrieves recent Safety Controller audit decisions and compliance justifications."""
    logs = db.get_recent_safety_logs(limit=limit)
    return json.dumps(logs, indent=2)


@server.tool()
def run_reconciliation_reaper() -> str:
    """Scans for and cleans up orphaned worker leases and abandoned calls from simulated crashes."""
    recovered_c, recovered_a = reaper.reconcile_stale_leases()
    return json.dumps({
        "status": "COMPLETED",
        "recovered_calls": recovered_c,
        "recovered_agents": recovered_a,
    }, indent=2)


@server.tool()
def inject_agent_drop(count: int = 20) -> str:
    """Simulates an abrupt drop in agent availability to verify Safety Controller fallback protection.

    Args:
        count: Number of currently available agents to immediately disconnect.
    """
    available = db.list_available_agents(limit=count)
    dropped = 0
    for a in available:
        db.update_agent_state(a.agent_id, AgentState.OFFLINE)
        dropped += 1
    return json.dumps({
        "status": "CHAOS_INJECTED",
        "dropped_agents": dropped,
        "message": f"Successfully offlined {dropped} agents abruptly. Next predictive tick will trigger FALLBACK_TO_PROGRESSIVE.",
    }, indent=2)


if __name__ == "__main__":
    # Runs the standard MCP stdio transport loop
    server.run()
