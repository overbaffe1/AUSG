"""Матрица прогонов: сценарий x цель x планировщик x режим x события.

Запуск:
    python experiments/run_benchmarks.py
    python experiments/run_benchmarks.py --full
    python experiments/run_benchmarks.py --scenarios P01_intro P02_shift
    python experiments/run_benchmarks.py --events examples/events_demo.json

``--full`` даёт полную матрицу раздела З7 одной командой:
{P01..P04} x {2 цели} x {baseline: edf|value|priority, economic}
x {centralized, federated} x {без событий, examples/events_demo.json}.

Результаты кладутся в experiments/results/ и используются в README.
Seed и все настройки фиксируются, чтобы сравнение было воспроизводимым.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.index import ScenarioIndex          # noqa: E402
from backend.core.scenario_store import STORE          # noqa: E402
from backend.planners.base import PlannerConfig        # noqa: E402
from backend.planners.baseline import BaselinePlanner  # noqa: E402
from backend.planners.economic import EconomicPlanner  # noqa: E402
from backend.planners.federated import FederatedPlanner  # noqa: E402
from model.operations import Session                   # noqa: E402

PLANNERS = {"baseline": BaselinePlanner, "economic": EconomicPlanner}
OBJECTIVES = ("priority_service", "commercial")
RESULTS = ROOT / "experiments" / "results"


def load_events(path: Path | None) -> dict[int, list[dict]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    events = payload["events"] if isinstance(payload, dict) else payload
    grouped: dict[int, list[dict]] = {}
    for event in events:
        grouped.setdefault(event["at_step"], []).append(event)
    return grouped


def run_once(scenario_id: str, planner_name: str, objective: str, rule: str,
             events: dict[int, list[dict]], mode: str = "centralized",
             replan_gap: int = 4, events_label: str = "нет") -> dict:
    scenario = json.loads(json.dumps(STORE.get(scenario_id).scenario))
    session = Session(scenario)
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(objective=objective, rule=rule, seed=0, mode=mode,
                        replan_gap_steps=replan_gap)
    if mode == "federated":
        planner = FederatedPlanner(horizon_steps=cfg.bundle_horizon_steps,
                                   replan_gap=replan_gap)
    else:
        planner = PLANNERS[planner_name]()

    started = time.perf_counter()
    applied = 0
    while session.env.k < index.steps:
        step = session.env.k
        for event in events.get(step, []):
            try:
                session.apply_event(event)
                applied += 1
                if event.get("type") == "add_jobs":
                    for job in event["jobs"]:
                        index.register(job)
            except ValueError:
                pass
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})
    elapsed = time.perf_counter() - started

    summary = session.summary()
    return {
        "scenario": scenario_id,
        "planner": planner_name,
        "mode": mode,
        "replan_gap": replan_gap if mode == "federated" else "-",
        "rule": rule if planner_name == "baseline" else "-",
        "objective": objective,
        "events": events_label,
        "events_applied": applied,
        "seconds": round(elapsed, 2),
        "revenue_usd": summary["revenue_usd"],
        "jobs_completed": summary["jobs_completed"],
        "jobs_due_missed": summary["jobs_due_missed"],
        "critical_due": summary["critical_jobs_due"],
        "critical_on_time": summary["critical_jobs_completed_on_time"],
        "work_steps_in_missed_jobs": summary["work_steps_in_missed_jobs"],
        "blocked_commands": summary["blocked_command_count"],
        "minimum_soc_pct": summary["minimum_soc_pct"],
    }


def full_matrix(scenarios: list[str], events: dict[int, list[dict]]) -> list[dict]:
    """Полная матрица раздела З7 — один прогон на каждую комбинацию."""
    rows: list[dict] = []
    event_sets = [({}, "нет")]
    if events:
        event_sets.append((events, "events_demo"))

    for scenario_id in scenarios:
        for objective in OBJECTIVES:
            for event_map, label in event_sets:
                for rule in ("edf", "value", "priority"):
                    rows.append(run_once(scenario_id, "baseline", objective, rule,
                                         event_map, events_label=label))
                rows.append(run_once(scenario_id, "economic", objective, "-",
                                     event_map, events_label=label))
                rows.append(run_once(scenario_id, "economic", objective, "-",
                                     event_map, mode="federated",
                                     events_label=label))
                for row in rows[-5:]:
                    print(_format(row))
    return rows


def _format(row: dict) -> str:
    return (f"{row['scenario']:<11} {row['planner']:<9} {row['rule']:<9}"
            f"{row['mode'][:5]:<6} {row['objective']:<17} "
            f"события {row['events']:<11} "
            f"{row['seconds']:>6.2f}s  ${row['revenue_usd']:>10,.0f}  "
            f"P3 {row['critical_on_time']:>4}/{row['critical_due']:<4} "
            f"просрочено {row['jobs_due_missed']:<5} "
            f"wsim {row['work_steps_in_missed_jobs']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", nargs="*", default=None)
    parser.add_argument("--events", type=Path, default=None)
    parser.add_argument("--rules", nargs="*", default=["edf"])
    parser.add_argument("--full", action="store_true",
                        help="полная матрица раздела З7 одной командой")
    parser.add_argument("--autonomy-sweep", nargs="*", type=int, default=None,
                        help="интервалы загрузки для замера цены автономии")
    args = parser.parse_args()

    scenarios = args.scenarios or [s["id"] for s in STORE.list()]
    events_path = args.events
    if args.full and events_path is None:
        events_path = ROOT / "examples" / "events_demo.json"
    events = load_events(events_path)

    if args.full:
        rows = full_matrix(scenarios, events)
        name = "benchmarks_full"
    else:
        rows = []
        for scenario_id in scenarios:
            for objective in OBJECTIVES:
                for rule in args.rules:
                    rows.append(run_once(scenario_id, "baseline", objective, rule,
                                         events))
                rows.append(run_once(scenario_id, "economic", objective, "-", events))
                for gap in (args.autonomy_sweep or []):
                    rows.append(run_once(scenario_id, "economic", objective, "-",
                                         events, mode="federated", replan_gap=gap))
                for row in rows[-(1 + len(args.rules) + len(args.autonomy_sweep or [])):]:
                    print(_format(row))
        name = "benchmarks"

    RESULTS.mkdir(parents=True, exist_ok=True)
    csv_path = RESULTS / f"{name}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (RESULTS / f"{name}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    slowest = max(rows, key=lambda r: r["seconds"])
    print(f"\nРезультаты: {csv_path} ({len(rows)} прогонов)")
    print(f"Самый долгий прогон: {slowest['scenario']} / {slowest['planner']} "
          f"/ {slowest['mode']} — {slowest['seconds']}s (бюджет 20s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
