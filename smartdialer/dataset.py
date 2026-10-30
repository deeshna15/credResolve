"""Dataset loader and statistics extractor from collections_30k_dataset.
Integrates borrowers, accounts, daily targeting, and historical call metrics into SmartDialer.
"""

from __future__ import annotations
import logging
import os
from typing import Dict, List, Optional, Tuple
import pandas as pd

from smartdialer.db import Database
from smartdialer.models import Agent, AgentState, Borrower

logger = logging.getLogger(__name__)


class DatasetLoader:
    """Loads records and historical telemetry from collections CSV files."""

    def __init__(self, data_dir: str = "."):
        self.data_dir = data_dir

    def extract_telephony_metrics(self) -> Dict[str, float]:
        """Extracts empirical baseline statistics from calls.csv."""
        calls_path = os.path.join(self.data_dir, "calls.csv")
        if not os.path.exists(calls_path):
            logger.warning(f"{calls_path} not found. Using default metrics.")
            return {"answer_rate": 0.20, "avg_talk_time_sec": 120.0}

        df = pd.read_csv(calls_path, nrows=50000)
        total_calls = len(df)
        if total_calls == 0:
            return {"answer_rate": 0.20, "avg_talk_time_sec": 120.0}

        # Answered status counts
        answered = df[df["call_status"].isin(["ANSWERED", "CONNECTED"])]
        answer_rate = len(answered) / float(total_calls)
        avg_duration = answered["duration_sec"].mean() if len(answered) > 0 else 120.0

        return {
            "answer_rate": float(answer_rate),
            "avg_talk_time_sec": float(avg_duration),
            "total_sample_calls": total_calls,
        }

    def load_agents(self, db: Database, limit: int = 100) -> int:
        """Loads active agents from agents.csv into the database."""
        agents_path = os.path.join(self.data_dir, "agents.csv")
        if not os.path.exists(agents_path):
            logger.warning("agents.csv not found; generating synthetic agents.")
            for i in range(1, limit + 1):
                db.upsert_agent(Agent(
                    agent_id=f"AGT_{i:04d}",
                    name=f"Agent {i}",
                    state=AgentState.AVAILABLE,
                ))
            return limit

        df = pd.read_csv(agents_path)
        loaded = 0
        for _, row in df.head(limit).iterrows():
            agent_id = str(row.get("agent_id", f"AGT_{loaded+1}"))
            name = str(row.get("agent_name", f"Agent {loaded+1}"))
            team = str(row.get("team", "T1"))
            skills = "SENIOR_RECOVERY,LEGAL" if team == "T3" else ("MID_STAGE,NEGOTIATION" if team == "T2" else "EARLY_STAGE,GENERAL")
            db.upsert_agent(Agent(
                agent_id=agent_id,
                name=name,
                state=AgentState.AVAILABLE,
                team=team,
                skills=skills,
            ))
            loaded += 1
        return loaded

    def load_borrowers(self, db: Database, limit: int = 1000) -> int:
        """Loads borrowers, joined with accounts and daily targeting, into prioritized queue."""
        borrowers_path = os.path.join(self.data_dir, "borrowers.csv")
        accounts_path = os.path.join(self.data_dir, "accounts.csv")
        targeting_path = os.path.join(self.data_dir, "daily_targeting.csv")

        if not os.path.exists(borrowers_path) or not os.path.exists(accounts_path):
            logger.warning("borrowers.csv or accounts.csv not found; generating synthetic borrowers.")
            for i in range(1, limit + 1):
                db.upsert_borrower(Borrower(
                    borrower_id=f"BRW_{i:06d}",
                    account_id=f"ACC_{i:06d}",
                    name=f"Borrower {i}",
                    phone=f"+9198{i:08d}",
                    dpd=30 + (i % 60),
                    outstanding_amount=50000.0 + (i * 100),
                    risk_segment="HIGH" if i % 3 == 0 else "MEDIUM",
                    priority=(i % 5) + 1,
                ))
            return limit

        df_b = pd.read_csv(borrowers_path, nrows=limit * 2)
        df_a = pd.read_csv(accounts_path, nrows=limit * 2)

        # Merge borrower and account data
        merged = pd.merge(df_b, df_a, on="borrower_id", how="inner").drop_duplicates(subset=["borrower_id"])

        # Prioritize high DPD, high outstanding balance, high risk
        loaded = 0
        for _, row in merged.head(limit).iterrows():
            dpd = int(row.get("dpd", 0))
            # Lower priority number = higher urgency
            priority = 1 if dpd > 60 else (2 if dpd > 30 else 3)
            db.upsert_borrower(Borrower(
                borrower_id=str(row["borrower_id"]),
                account_id=str(row.get("account_id", f"ACC_{loaded}")),
                name=str(row.get("name", f"Borrower {loaded}")),
                phone=str(row.get("phone", f"+9190000{loaded:05d}")),
                dpd=dpd,
                outstanding_amount=float(row.get("outstanding_amount", 10000.0)),
                risk_segment=str(row.get("risk_segment", "MEDIUM")),
                priority=priority,
            ))
            loaded += 1
        return loaded
