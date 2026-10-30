"""Interactive Streamlit Dashboard for SmartDialer.

Visualizes progressive/predictive dialing, safety controller decisions,
agent state lifecycles, and failure injections in real-time.
"""

from datetime import datetime, timezone
import json
import streamlit as st
import pandas as pd
import time

from smartdialer.campaign import CampaignEngine
from smartdialer.dataset import DatasetLoader
from smartdialer.db import Database
from smartdialer.models import Agent, AgentState, Borrower, PacingMode, SafetyAction
from smartdialer.reaper import ReconciliationReaper
from smartdialer.telecom import CircuitState, ProviderA, ProviderB


# Page configuration
st.set_page_config(
    page_title="SmartDialer | Collections Decision Engine",
    page_icon="📞",
    layout="wide",
)

st.title("📞 SmartDialer: Intelligent Collections Dialing Platform")
st.caption("Demonstrating Progressive Dialing, Predictive Pacing, and Deterministic Safety Controller Boundaries.")


@st.cache_resource
def get_shared_system():
    db = Database(":memory:")
    loader = DatasetLoader()
    loader.load_agents(db, limit=50)
    loader.load_borrowers(db, limit=500)
    provider = ProviderA(answer_rate=0.20, avg_talk_time_sec=120.0)
    engine = CampaignEngine(
        db=db,
        campaign_id="CAMP_DASHBOARD",
        provider=provider,
        mode=PacingMode.PROGRESSIVE,
    )
    reaper = ReconciliationReaper(db)
    return db, provider, engine, reaper


db, provider, engine, reaper = get_shared_system()

# Sidebar: Controls
st.sidebar.header("⚙️ Campaign Control")
selected_mode = st.sidebar.selectbox(
    "Dialing Mode",
    options=[PacingMode.PROGRESSIVE.value, PacingMode.PREDICTIVE.value],
    index=0,
)
engine.mode = PacingMode(selected_mode)

selected_provider = st.sidebar.selectbox(
    "Telecom Provider",
    options=["Provider A (Fast & Reliable)", "Provider B (Slow, Chaos, Webhook Dups)"],
    index=0,
)
if "Provider B" in selected_provider:
    engine.provider = ProviderB(answer_rate=0.20, avg_talk_time_sec=120.0, timeout_rate=0.15)
else:
    engine.provider = ProviderA(answer_rate=0.20, avg_talk_time_sec=120.0)

st.sidebar.markdown("---")
st.sidebar.subheader("Campaign Action")

col_sb1, col_sb2 = st.sidebar.columns(2)
if col_sb1.button("▶️ Step 1 Tick", use_container_width=True):
    engine.execute_tick()
    st.rerun()

if col_sb2.button("🧹 Run Reaper", use_container_width=True):
    recov_c, recov_a = reaper.reconcile_stale_leases()
    st.sidebar.success(f"Recovered {recov_c} calls, {recov_a} agents.")
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("🚨 Failure Injections")

if st.sidebar.button("💥 Drop 40% Agents (Sudden Drop)"):
    available = db.list_available_agents(limit=20)
    for a in available:
        db.update_agent_state(a.agent_id, AgentState.OFFLINE)
    st.sidebar.warning(f"Offline {len(available)} agents abruptly!")
    st.rerun()

if st.sidebar.button("⚡ Trip Provider Circuit Breaker"):
    engine.provider.consecutive_errors = 5
    engine.provider.total_failed += 20
    st.sidebar.error("Injected consecutive provider failures. Circuit breaker tripped!")
    st.rerun()

if st.sidebar.button("🔄 Reset Campaign"):
    st.cache_resource.clear()
    st.rerun()


# Main View: Live Telemetry
metrics = db.get_operational_metrics()

m_col1, m_col2, m_col3, m_col4, m_col5 = st.columns(5)
m_col1.metric("Total Agents", metrics["total_agents"])
m_col2.metric("Available Agents", metrics["available_agents"])
m_col3.metric("Agent Utilization", f"{metrics['agent_utilization']:.1%}")
m_col4.metric("Active Ringing Calls", metrics["ringing_calls"])

aband_rate = metrics["abandon_rate"]
aband_delta = "- SAFE" if aband_rate <= 0.03 else "⚠️ NON-COMPLIANT (>3%)"
m_col5.metric("Abandon Rate", f"{aband_rate:.2%}", delta=aband_delta)

st.markdown("---")

tab_pacing, tab_safety, tab_agents, tab_calls, tab_simulation, tab_architecture = st.tabs([
    "🎯 Pacing & Explainability",
    "🛡️ Safety Controller Boundary",
    "👥 Live Agent Grid & Add Agent",
    "📋 Call Queue & States",
    "📊 Live Scenario Simulator",
    "🏛️ System Architecture",
])

with tab_pacing:
    st.subheader("Pacing Engine Formula & Decision Log")
    recent_logs = db.get_recent_safety_logs(limit=1)
    if recent_logs:
        last = recent_logs[0]
        st.info(f"**Current Pacing Mode:** `{last['active_mode']}` | **Safety Action:** `{last['action']}`")
        
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("##### 🧮 Mathematical Pacing Inputs")
            st.json(last["metrics"])
        with c2:
            st.markdown("##### 📝 Decision Reason & Audit Trail")
            st.success(last["reason"])
    else:
        st.write("Click **'▶️ Step 1 Tick'** in the sidebar to generate live pacing decisions.")

with tab_safety:
    st.subheader("Non-Bypassable Safety Controller Audit Log")
    st.markdown("""
    The Safety Controller sits between the Pacing Engine and the Telecom Call Allocator:
    `Campaign > Pacing Engine > Safety Controller > Call Allocator > Telecom Provider`
    It independently verifies compliance bounds, agent drop rates, and circuit breaker health.
    """)
    safety_logs = db.get_recent_safety_logs(limit=20)
    if safety_logs:
        df_logs = pd.DataFrame(safety_logs)
        st.dataframe(df_logs[["id", "timestamp", "action", "requested_calls", "authorized_calls", "active_mode", "reason"]], use_container_width=True)
    else:
        st.write("No safety evaluations logged yet.")

with tab_agents:
    st.subheader("Live Agent States, Skills & Management")

    # Add Agent Form
    with st.expander("➕ Add New Collections Agent to Pool", expanded=False):
        with st.form("add_agent_form"):
            c_a1, c_a2 = st.columns(2)
            new_id = c_a1.text_input("Agent ID", value=f"AGT_{int(time.time()) % 10000:04d}")
            new_name = c_a2.text_input("Agent Name", placeholder="e.g. Priya Sharma")

            c_a3, c_a4 = st.columns(2)
            new_team = c_a3.selectbox("Recovery Tier / Team", options=["T1 (Early Delinquency)", "T2 (Mid Stage DPD)", "T3 (Senior Legal / High DPD)"])
            new_skills = c_a4.text_input("Skills / Languages", value="HINDI, ENGLISH, NEGOTIATION")

            submitted = st.form_submit_button("Add Agent to Dialer Pool", use_container_width=True)
            if submitted and new_name:
                team_code = new_team[:2]
                db.upsert_agent(Agent(
                    agent_id=new_id,
                    name=new_name,
                    state=AgentState.AVAILABLE,
                    team=team_code,
                    skills=new_skills,
                ))
                st.success(f"Agent {new_name} ({new_id}) successfully enrolled in {team_code}!")
                st.rerun()

    agent_counts = metrics["agents"]
    st.bar_chart(pd.DataFrame(list(agent_counts.items()), columns=["State", "Count"]).set_index("State"))

    with db.get_connection() as conn:
        df_agents = pd.read_sql("SELECT agent_id, name, state, team, skills, version, current_call_id, total_calls_handled FROM agents ORDER BY updated_at DESC LIMIT 40;", conn)
        st.dataframe(df_agents, use_container_width=True)

with tab_calls:
    st.subheader("Call Queue & Outcomes")
    call_counts = metrics["calls"]
    st.write(call_counts)

    with db.get_connection() as conn:
        df_calls = pd.read_sql("SELECT call_id, phone, agent_id, state, mode, duration_sec, hangup_reason, lease_expires_at FROM calls ORDER BY created_at DESC LIMIT 25;", conn)
        st.dataframe(df_calls, use_container_width=True)

with tab_simulation:
    st.subheader("📊 Live Benchmark Simulation (Scenarios A - D)")
    st.markdown("Run automated dialing simulations to observe how Progressive vs Predictive modes perform under varying answer rates, talk times, and telecom provider anomalies.")

    sim_sc = st.selectbox(
        "Select Simulation Scenario",
        options=[
            "Scenario A (20% Answer Rate, 120s AHT - Low Connect Rate)",
            "Scenario B (50% Answer Rate, 90s AHT - Balanced Collections)",
            "Scenario C (70% Answer Rate, 180s AHT - High Connect Rate)",
            "Scenario D (Dynamic Drift & Provider B Chaos)",
        ],
    )
    sim_mode = st.radio("Simulation Mode", options=["PREDICTIVE", "PROGRESSIVE"], horizontal=True)

    if st.button("🚀 Run Live Simulation (25 Cycles)", use_container_width=True):
        from simulation import SmartDialerSimulator, SimulationScenario
        sim = SmartDialerSimulator()

        if "Scenario A" in sim_sc:
            sc_obj = SimulationScenario("Scenario A", "20% Ans Rate", 0.20, 120.0, 15.0, "A", ticks=25)
        elif "Scenario B" in sim_sc:
            sc_obj = SimulationScenario("Scenario B", "50% Ans Rate", 0.50, 90.0, 15.0, "A", ticks=25)
        elif "Scenario C" in sim_sc:
            sc_obj = SimulationScenario("Scenario C", "70% Ans Rate", 0.70, 180.0, 15.0, "A", ticks=25)
        else:
            sc_obj = SimulationScenario("Scenario D", "Dynamic Drift", 0.40, 110.0, 18.0, "B", dynamic_drift=True, ticks=25)

        with st.spinner("Executing simulation ticks..."):
            sim_res = sim.run_scenario(sc_obj, PacingMode(sim_mode))

        st.success(f"Simulation completed for {sc_obj.name} in {sim_mode} mode!")
        c_r1, c_r2, c_r3, c_r4 = st.columns(4)
        c_r1.metric("Calls Initiated", sim_res["calls_initiated"])
        c_r2.metric("Calls Connected", sim_res["calls_connected"])
        c_r3.metric("Calls Abandoned", sim_res["calls_abandoned"])
        c_r4.metric("Average Abandon Rate", f"{sim_res['abandon_rate']:.2%}")

        st.markdown("##### 🛡️ Safety Controller Decisions Breakdown")
        st.write(sim_res["safety_decisions"])

with tab_architecture:
    st.subheader("CredResolve SmartDialer Architecture")
    st.markdown("""
    ```mermaid
    flowchart LR
        subgraph Orchestration
            C[Campaign Engine] --> PE[Pacing Engine\nProgressive / Predictive]
            PE --> SC{Safety Controller\nBoundary}
        end

        subgraph Safety Rules
            SC -->|Approve / Reduce| CA[Call Allocator]
            SC -->|Fallback| PE_Prog[Progressive Fallback]
            SC -->|Reject| RJ[0 Dials Allowed]
        end

        subgraph Concurrency & Storage
            CA --> DB[(SQLite / PostgreSQL\nAtomic CAS & Leases)]
            R[Reconciliation Reaper] -.->|Heartbeat / Lease Checks| DB
        end

        subgraph Telephony
            CA --> TP[Telecom Provider\nA: Reliable / B: Degraded]
            TP -->|Idempotent Webhooks| CA
        end
    ```
    """)
