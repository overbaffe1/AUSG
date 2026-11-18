"""Замер нестабильности решений (З3).

Метрика: доля шагов, на которых аппарат сменил задание, НЕ ЗАВЕРШИВ
предыдущее. Именно такая смена — чистый убыток: вложенные шаги пропадают,
частичная работа не оплачивается.

Дополнительно печатается разброс теневых цен (среднее абсолютное
изменение lambda_W между соседними шагами) — это то, что сглаживание
правит напрямую.

Запуск:
    python experiments/stability.py
    python experiments/stability.py --scenarios P02_shift --alphas 1.0 0.5 0.3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.index import ScenarioIndex          # noqa: E402
from backend.core.scenario_store import STORE          # noqa: E402
from backend.planners.base import PlannerConfig        # noqa: E402
from backend.planners.economic import EconomicPlanner  # noqa: E402
from model.operations import Session                   # noqa: E402

RESULTS = ROOT / "experiments" / "results"


def measure(scenario_id: str, objective: str, alpha: float) -> dict:
    scenario = json.loads(json.dumps(STORE.get(scenario_id).scenario))
    session = Session(scenario)
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(objective=objective, price_smoothing=alpha)
    planner = EconomicPlanner()

    previous_job: dict[str, str] = {}
    previous_prices: dict[str, float] = {}
    switches = 0
    working_steps = 0
    price_jump = 0.0
    price_samples = 0

    while session.env.k < index.steps:
        decisions = planner.decide(session, index, cfg)
        for sid, decision in decisions.items():
            job_id = decision.action.get("job_id")
            if job_id:
                working_steps += 1
            was = previous_job.get(sid)
            if was and job_id and job_id != was \
                    and session.env.jobs[was]["completed_step"] is None:
                switches += 1
            if job_id:
                previous_job[sid] = job_id
            elif was and session.env.jobs[was]["completed_step"] is not None:
                previous_job.pop(sid, None)

            price = decision.explain.get("shadow_prices", {}).get("sat_step")
            if price is not None:
                if sid in previous_prices:
                    price_jump += abs(price - previous_prices[sid])
                    price_samples += 1
                previous_prices[sid] = price
        session.advance({sid: d.action for sid, d in decisions.items()})

    summary = session.summary()
    return {
        "scenario": scenario_id,
        "objective": objective,
        "price_smoothing": alpha,
        "switches_before_completion": switches,
        "working_steps": working_steps,
        "instability_pct": round(100.0 * switches / max(1, working_steps), 3),
        "mean_lambda_w_jump": round(price_jump / max(1, price_samples), 4),
        "revenue_usd": summary["revenue_usd"],
        "critical_on_time": summary["critical_jobs_completed_on_time"],
        "critical_due": summary["critical_jobs_due"],
        "work_steps_in_missed_jobs": summary["work_steps_in_missed_jobs"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", nargs="*",
                        default=["P02_shift", "P03_energy", "P04_demand"])
    parser.add_argument("--objectives", nargs="*",
                        default=["priority_service", "commercial"])
    parser.add_argument("--alphas", nargs="*", type=float,
                        default=[1.0, 0.6, 0.4, 0.2])
    args = parser.parse_args()

    rows = [measure(scenario_id, objective, alpha)
            for scenario_id in args.scenarios
            for objective in args.objectives
            for alpha in args.alphas]

    for row in rows:
        print(f"{row['scenario']:<11} {row['objective']:<17} "
              f"alpha={row['price_smoothing']:<4} "
              f"нестабильность {row['instability_pct']:>6.2f}%  "
              f"скачок lambda_W {row['mean_lambda_w_jump']:>7.3f}  "
              f"${row['revenue_usd']:>10,.0f}  "
              f"P3 {row['critical_on_time']}/{row['critical_due']}  "
              f"wsim {row['work_steps_in_missed_jobs']}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "stability.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\nРезультаты: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
