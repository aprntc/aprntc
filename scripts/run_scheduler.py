"""Run aprntc's autonomous improvement loop on a schedule.

For EVERY registered agent, on a cadence, this runs the full pipeline:
  distill lessons → write to memory → build candidate playbook → replay-gate →
  write a review bundle (or auto-promote if A4 is enabled and guardrails clear).

It only does real work for an agent when enough NEW trajectories have accumulated
since its last run (the orchestrator's trigger), so running it frequently is cheap.

In production this runs as a long-lived process (or call scheduler.run_due() from
cron). Needs ModelArk keys.

  .venv/bin/python scripts/run_scheduler.py                                # every 24h
  APRNTC_IMPROVE_INTERVAL_S=120 .venv/bin/python scripts/run_scheduler.py  # demo: every 2 min
"""

from __future__ import annotations

import os
import sys
import time

from aprntc.ops import METRICS, Scheduler, with_retry
from aprntc.serving import PlaybookRegistry
from aprntc.web.app import _build_orchestrator_engine, _load_auto_policy


def main() -> int:
    engine = _build_orchestrator_engine()
    if engine is None:
        print("[skip] ModelArk keys not configured — can't run the improvement engine.")
        return 1

    from aprntc.eval.fusion import compute_trust
    from aprntc.memory import make_memory_store
    from aprntc.serving.orchestrator import ImprovementOrchestrator
    from aprntc.trajectory.store import make_trajectory_store

    store = make_trajectory_store(os.environ.get("APRNTC_DB_URL") or "aprntc.db")
    memory = make_memory_store(os.environ.get("APRNTC_VECTOR_DB_URL") or "local:///./aprntc_memory.db")
    playbooks = PlaybookRegistry("playbooks.json")
    interval = float(os.environ.get("APRNTC_IMPROVE_INTERVAL_S", 24 * 3600))
    min_new = int(os.environ.get("APRNTC_IMPROVE_MIN_NEW", 5))

    def build_orch() -> ImprovementOrchestrator:
        policy = _load_auto_policy("auto_policy.json")           # default-off unless enabled
        all_labels = [store.labels_for(e.episode_id) for e in store.query()]
        trust = compute_trust(all_labels)
        return ImprovementOrchestrator(
            store=store, memory=memory, playbooks=playbooks,
            provider=engine["provider"], distiller=engine["distiller"],
            judge=engine["judge"], model=engine["model"],
            bundle_path="review_bundle.json", state_path="orchestrator_state.json",
            min_new_trajectories=min_new,
            auto_policy=policy, trust=trust.value if trust else None,
        )

    def improvement_tick() -> None:
        orch = build_orch()
        agents = list(playbooks.agent_ids())
        if not agents:
            print("[improve] no agents registered yet.")
            return
        for agent_id in agents:
            report = with_retry(lambda aid=agent_id: orch.run_for_agent(aid), attempts=2)
            if report.ran:
                METRICS.incr("improve.runs", 1)
                METRICS.incr("improve.lessons", report.n_lessons)
                print(f"[improve] {agent_id}: mined {report.n_lessons} lessons → "
                      f"G{report.candidate_generation} "
                      f"(win={report.win_rate}, passed={report.gate_passed}, "
                      f"auto_promoted={report.auto_promoted})")
            else:
                print(f"[improve] {agent_id}: skipped — {report.reason}")

    sch = Scheduler()
    sch.add("improvement_loop", improvement_tick, interval_s=interval, run_immediately=True)
    print(f"Improvement scheduler running: every {interval:.0f}s "
          f"(min {min_new} new trajectories per agent). Ctrl-C to stop.")
    sch.start(tick_s=1.0)
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        sch.stop()
        try:
            engine["provider"].close()
            store.close()
        except Exception:
            pass
        print("\nstopped.", "metrics:", METRICS.snapshot()["counters"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
