"""Tests for concurrency, atomic CAS reservations, and worker contention.
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from smartdialer.db import Database
from smartdialer.models import Agent, AgentState, Borrower


class TestConcurrencyAndCAS(unittest.TestCase):

    def setUp(self):
        # In-memory database with shared connection logic
        self.db = Database(":memory:")

    def test_two_workers_reserve_same_agent_at_same_time(self):
        """CRITICAL TEST: Two workers see the same available agent at almost the same time.

        Both MUST NOT be able to reserve that agent. Exactly ONE succeeds.
        """
        agent_id = "AGT_RACE_001"
        self.db.upsert_agent(Agent(
            agent_id=agent_id,
            name="Alice Concurrency",
            state=AgentState.AVAILABLE,
            version=1,
        ))

        # Both workers inspect agent at version 1
        agent = self.db.get_agent(agent_id)
        self.assertIsNotNone(agent)
        self.assertEqual(agent.version, 1)

        results = []
        barrier = threading.Barrier(2)

        def worker_attempt(worker_id: str):
            barrier.wait()  # synchronize exact simultaneous execution
            success = self.db.try_atomic_reserve_agent(
                agent_id=agent_id,
                expected_version=agent.version,
                worker_id=worker_id,
            )
            results.append((worker_id, success))

        t1 = threading.Thread(target=worker_attempt, args=("WORKER_A",))
        t2 = threading.Thread(target=worker_attempt, args=("WORKER_B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r[1] is True]
        failures = [r for r in results if r[1] is False]

        self.assertEqual(len(successes), 1, "Exactly one worker MUST succeed in reserving the agent.")
        self.assertEqual(len(failures), 1, "The losing worker MUST fail safely without corrupting state.")

        # Verify DB state
        updated_agent = self.db.get_agent(agent_id)
        self.assertEqual(updated_agent.state, AgentState.RESERVED)
        self.assertEqual(updated_agent.version, 2)
        self.assertEqual(updated_agent.reserved_by, successes[0][0])

    def test_ten_workers_competing_for_single_agent(self):
        agent_id = "AGT_CONGESTED_001"
        self.db.upsert_agent(Agent(
            agent_id=agent_id,
            name="Bob HighContention",
            state=AgentState.AVAILABLE,
            version=1,
        ))

        agent = self.db.get_agent(agent_id)
        results = []
        barrier = threading.Barrier(10)

        def worker_attempt(idx: int):
            barrier.wait()
            success = self.db.try_atomic_reserve_agent(
                agent_id=agent_id,
                expected_version=1,
                worker_id=f"WORKER_{idx}",
            )
            results.append(success)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker_attempt, i) for i in range(10)]
            for f in futures:
                f.result()

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 9)

    def test_concurrent_borrower_queue_allocation(self):
        """Ensures 10 concurrent threads dequeue unique borrowers without duplicate calls."""
        for i in range(20):
            self.db.upsert_borrower(Borrower(
                borrower_id=f"BRW_{i:03d}",
                account_id=f"ACC_{i:03d}",
                name=f"Borrower {i}",
                phone=f"+9198000000{i:02d}",
                dpd=30 + i,
                priority=1,
            ))

        allocated_borrowers = []
        lock = threading.Lock()

        def worker_dequeue(idx: int):
            b = self.db.fetch_and_reserve_next_borrower(worker_id=f"WORKER_{idx}")
            if b:
                with lock:
                    allocated_borrowers.append(b.borrower_id)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker_dequeue, i) for i in range(20)]
            for f in futures:
                f.result()

        # Check for zero duplicates
        self.assertEqual(len(allocated_borrowers), len(set(allocated_borrowers)))


if __name__ == "__main__":
    unittest.main()
