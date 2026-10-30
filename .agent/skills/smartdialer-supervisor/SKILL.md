---
name: smartdialer-supervisor
description: Autonomous supervisor and compliance auditor for the CredResolve SmartDialer collections engine.
---

# SmartDialer Supervisor & Compliance Skill

Use this skill to supervise, configure, and audit collections campaigns operating on the SmartDialer platform.

## When to Use This Skill
- When the user asks to monitor agent utilization, active calls, or compliance metrics.
- When evaluating or triggering dialing cycles in Progressive or Predictive mode.
- When investigating statutory abandon rate violations or testing failure scenarios.
- When adding new agents or assigning recovery tiers (T1/T2/T3).

## Operational Guidelines

### 1. Statutory Compliance Invariant
- Regulatory threshold: The rolling abandon rate must never exceed **3.0%**.
- If abandon rate approaches 3.0%, enforce `FALLBACK_TO_PROGRESSIVE` mode.
- Progressive mode maintains a strict 1:1 agent-to-call ratio (0.00% abandon risk).

### 2. Available Commands & Scripts
- **Run Unit Tests:** `python -m unittest discover tests`
- **Run Scenario Benchmarks:** `python simulation.py`
- **Run Concurrency Load Test:** `python load_test.py`
- **Launch Web Dashboard:** `streamlit run app.py`
- **Launch REST API:** `python -m uvicorn api:app --port 8000`
- **Run MCP Server:** `python mcp_server.py`

### 3. Agent Lifecycle Ranks
Agents strictly transition through:
`AVAILABLE` -> `RESERVED` -> `DIALING` -> `CONNECTED` -> `WRAP_UP` -> `AVAILABLE`.
Races between concurrent workers are protected by atomic Compare-And-Swap (OCC).
