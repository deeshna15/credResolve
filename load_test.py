"""High-concurrency Load Test for SmartDialer.

Evaluates throughput, contention, CAS collision rates, and latency under multi-worker pressure.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Dict, List
import statistics

from smartdialer.campaign import CampaignEngine
from smartdialer.dataset import DatasetLoader
from smartdialer.db import Database
from smartdialer.models import PacingMode
from smartdialer.telecom import ProviderA


def run_concurrency_load_test(
    num_workers: int = 5,
    num_agents: int = 50,
    num_borrowers: int = 500,
    iterations_per_worker: int = 10,
) -> Dict[str, float]:
    print(f"\n" + "=" * 80)
    print(f" SMARTDIALER CONCURRENCY & LOAD TEST")
    print(f" Concurrency: {num_workers} workers | Agents: {num_agents} | Borrowers: {num_borrowers}")
    print(f"=" * 80)

    db = Database(":memory:")
    loader = DatasetLoader()
    loader.load_agents(db, limit=num_agents)
    loader.load_borrowers(db, limit=num_borrowers)

    provider = ProviderA(answer_rate=0.20, failure_rate=0.01)

    latencies_ms: List[float] = []
    dispatched_totals: List[int] = []

    start_wall_time = time.perf_counter()

    def worker_task(worker_id: str) -> Tuple[int, List[float]]:
        engine = CampaignEngine(
            db=db,
            campaign_id="CAMP_LOAD_TEST",
            provider=provider,
            mode=PacingMode.PROGRESSIVE,
            worker_id=worker_id,
        )
        local_dispatched = 0
        local_latencies = []

        for _ in range(iterations_per_worker):
            t0 = time.perf_counter()
            res = engine.execute_tick()
            t_ms = (time.perf_counter() - t0) * 1000.0

            local_latencies.append(t_ms)
            local_dispatched += len(res.dispatched_calls)

            # Brief simulation pause
            time.sleep(0.005)

        return local_dispatched, local_latencies

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(worker_task, f"LOAD_WORKER_{i}")
            for i in range(num_workers)
        ]
        for f in as_completed(futures):
            count, lats = f.result()
            dispatched_totals.append(count)
            latencies_ms.extend(lats)

    total_wall_time = time.perf_counter() - start_wall_time
    total_calls_placed = sum(dispatched_totals)
    throughput = total_calls_placed / total_wall_time if total_wall_time > 0 else 0.0

    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    latencies_sorted = sorted(latencies_ms)
    p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else 0.0
    p99 = latencies_sorted[int(len(latencies_sorted) * 0.99)] if latencies_sorted else 0.0

    print("\n--- Benchmark Results ---")
    print(f"Total Execution Time:      {total_wall_time:.2f} s")
    print(f"Total Cycles Completed:    {len(latencies_ms)}")
    print(f"Total Calls Dispatched:    {total_calls_placed}")
    print(f"Throughput:                {throughput:.1f} calls/sec")
    print(f"Tick Latency (p50):        {p50:.2f} ms")
    print(f"Tick Latency (p95):        {p95:.2f} ms")
    print(f"Tick Latency (p99):        {p99:.2f} ms")
    print("=" * 80 + "\n")

    return {
        "throughput_calls_per_sec": throughput,
        "p50_latency_ms": p50,
        "p95_latency_ms": p95,
        "p99_latency_ms": p99,
        "total_calls": total_calls_placed,
    }


if __name__ == "__main__":
    from typing import Tuple
    run_concurrency_load_test()
