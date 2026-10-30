"""Simulation runner for SmartDialer across Scenarios A, B, C, and D.

Compares Progressive vs Predictive modes and demonstrates how the Safety Controller
adapts under changing answer rates, talk times, and provider failures.
"""

from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import random
import time
from typing import Any, Dict, List

from smartdialer.campaign import CampaignEngine
from smartdialer.dataset import DatasetLoader
from smartdialer.db import Database
from smartdialer.models import Agent, AgentState, Borrower, PacingMode, SafetyAction
from smartdialer.telecom import ProviderA, ProviderB


@dataclass
class SimulationScenario:
    name: str
    description: str
    answer_rate: float
    avg_talk_time_sec: float
    avg_ring_time_sec: float
    provider_type: str  # "A" or "B"
    dynamic_drift: bool = False
    num_agents: int = 50
    num_borrowers: int = 500
    ticks: int = 30


class SmartDialerSimulator:
    """Executes multi-scenario simulations comparing progressive and predictive dialing."""

    def __init__(self, data_dir: str = "."):
        self.loader = DatasetLoader(data_dir=data_dir)

    def run_scenario(
        self,
        scenario: SimulationScenario,
        mode: PacingMode,
    ) -> Dict[str, Any]:
        # Initialize fresh in-memory database for simulation run
        db = Database(":memory:")

        # Load agents
        self.loader.load_agents(db, limit=scenario.num_agents)
        # Load prioritized borrowers
        self.loader.load_borrowers(db, limit=scenario.num_borrowers)

        # Setup provider
        if scenario.provider_type == "B":
            provider = ProviderB(
                answer_rate=scenario.answer_rate,
                avg_talk_time_sec=scenario.avg_talk_time_sec,
                timeout_rate=0.08,
                duplicate_event_rate=0.10,
                out_of_order_rate=0.05,
            )
        else:
            provider = ProviderA(
                answer_rate=scenario.answer_rate,
                avg_talk_time_sec=scenario.avg_talk_time_sec,
                failure_rate=0.01,
            )

        engine = CampaignEngine(
            db=db,
            campaign_id=f"SIM_{scenario.name}_{mode.value}",
            provider=provider,
            mode=mode,
            target_abandon_rate=0.03,
            confidence_damping=0.85,
        )

        safety_counts = {action: 0 for action in SafetyAction}
        pacing_history = []
        utilization_history = []
        abandon_rate_history = []

        total_dispatched = 0

        # Run simulation ticks
        for tick in range(scenario.ticks):
            # Dynamic drift simulation for Scenario D
            if scenario.dynamic_drift:
                if tick < 10:
                    provider.answer_rate = 0.70
                    provider.avg_talk_time_sec = 60.0
                elif tick < 20:
                    # Sudden drop in answer rate! (e.g. 70% -> 15%)
                    provider.answer_rate = 0.15
                    provider.avg_talk_time_sec = 180.0
                else:
                    provider.answer_rate = 0.40
                    provider.avg_talk_time_sec = 110.0

            # Execute dial cycle
            result = engine.execute_tick()
            safety_counts[result.safety_eval.action] += 1
            total_dispatched += len(result.dispatched_calls)

            pacing_history.append(result.recommendation.requested_calls)
            metrics = result.metrics_snapshot
            utilization_history.append(metrics["agent_utilization"])
            abandon_rate_history.append(metrics["abandon_rate"])

            # Free up agents that finish their calls
            self._simulate_agent_lifecycle_advancement(db, scenario.avg_talk_time_sec)

        final_metrics = db.get_operational_metrics()
        avg_utilization = (
            sum(utilization_history) / len(utilization_history)
            if utilization_history
            else 0.0
        )

        return {
            "scenario": scenario.name,
            "mode": mode.value,
            "total_agents": scenario.num_agents,
            "ticks": scenario.ticks,
            "calls_initiated": total_dispatched,
            "calls_connected": final_metrics["calls"].get("CONNECTED", 0) + final_metrics["calls"].get("COMPLETED", 0),
            "calls_abandoned": final_metrics["abandoned_calls"],
            "abandon_rate": final_metrics["abandon_rate"],
            "avg_utilization": avg_utilization,
            "safety_decisions": {k.value: v for k, v in safety_counts.items()},
        }

    def _simulate_agent_lifecycle_advancement(
        self, db: Database, avg_talk_time_sec: float
    ) -> None:
        """Simulates passage of time: agents in CONNECTED finish calls and WRAP_UP agents become AVAILABLE."""
        with db.get_connection() as conn:
            # Agents in WRAP_UP become AVAILABLE
            conn.execute("""
                UPDATE agents
                SET state = 'AVAILABLE', current_call_id = NULL, reserved_by = NULL
                WHERE state = 'WRAP_UP';
            """)

            # Stochastic completion of active CONNECTED calls based on duration
            cur = conn.execute("SELECT agent_id, current_call_id FROM agents WHERE state = 'CONNECTED';")
            active = cur.fetchall()

            for row in active:
                # 35% chance per tick of finishing call
                if random.random() < 0.35:
                    agent_id = row["agent_id"]
                    call_id = row["current_call_id"]
                    if call_id:
                        db.transition_call(
                            call_id=call_id,
                            target_state=db.get_call(call_id).state if not db.get_call(call_id) else db.get_call(call_id).state,
                            duration_sec=avg_talk_time_sec,
                        )
                    db.update_agent_state(agent_id, AgentState.WRAP_UP)


def run_all_benchmark_scenarios() -> List[Dict[str, Any]]:
    scenarios = [
        SimulationScenario(
            name="Scenario A",
            description="Answer Rate 20%, Avg Talk Time 120s (Low answer rate)",
            answer_rate=0.20,
            avg_talk_time_sec=120.0,
            avg_ring_time_sec=15.0,
            provider_type="A",
            ticks=25,
        ),
        SimulationScenario(
            name="Scenario B",
            description="Answer Rate 50%, Avg Talk Time 90s (Balanced collections)",
            answer_rate=0.50,
            avg_talk_time_sec=90.0,
            avg_ring_time_sec=15.0,
            provider_type="A",
            ticks=25,
        ),
        SimulationScenario(
            name="Scenario C",
            description="Answer Rate 70%, Avg Talk Time 180s (High answer rate)",
            answer_rate=0.70,
            avg_talk_time_sec=180.0,
            avg_ring_time_sec=15.0,
            provider_type="A",
            ticks=25,
        ),
        SimulationScenario(
            name="Scenario D",
            description="Dynamic Drift (Changing answer rate & Provider B Chaos)",
            answer_rate=0.40,
            avg_talk_time_sec=110.0,
            avg_ring_time_sec=18.0,
            provider_type="B",
            dynamic_drift=True,
            ticks=30,
        ),
    ]

    simulator = SmartDialerSimulator()
    results = []

    print("\n" + "=" * 90)
    print(" SMARTDIALER BENCHMARK SIMULATION: PROGRESSIVE VS PREDICTIVE MODES")
    print("=" * 90)

    for sc in scenarios:
        print(f"\n>>> Running {sc.name}: {sc.description}")
        res_prog = simulator.run_scenario(sc, PacingMode.PROGRESSIVE)
        res_pred = simulator.run_scenario(sc, PacingMode.PREDICTIVE)
        results.extend([res_prog, res_pred])

    # Print formatted comparative table
    print("\n" + "=" * 105)
    print(f"{'Scenario':<12} | {'Mode':<11} | {'Initiated':<10} | {'Connected':<10} | {'Abandoned':<10} | {'Aband %':<8} | {'Utilization':<11} | {'Safety Decisions (App/Red/Rej/Fall)'}")
    print("-" * 105)

    for r in results:
        dec = r["safety_decisions"]
        dec_str = f"{dec.get('APPROVE',0)} / {dec.get('REDUCE',0)} / {dec.get('REJECT',0)} / {dec.get('FALLBACK_TO_PROGRESSIVE',0)}"
        print(
            f"{r['scenario']:<12} | "
            f"{r['mode']:<11} | "
            f"{r['calls_initiated']:<10} | "
            f"{r['calls_connected']:<10} | "
            f"{r['calls_abandoned']:<10} | "
            f"{r['abandon_rate']:<7.2%} | "
            f"{r['avg_utilization']:<10.1%} | "
            f"{dec_str}"
        )
    print("=" * 105 + "\n")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SmartDialer Simulation Runner")
    parser.add_argument("--data-dir", default=".", help="Directory containing collections CSV files")
    args = parser.parse_args()

    run_all_benchmark_scenarios()
