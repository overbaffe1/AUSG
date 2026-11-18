"""Цена защиты шлюза (З8.4) — измеряем, а не считаем данностью.

Правило «хотя бы один аппарат остаётся на связи» взято из устной реплики
кейсодержателя (00:05:25) и в расчётной модели НЕ определено: `idle` — это
ожидание, а не операция «поддерживать связь». Мы ввели его сами, флагом
`protect_gateway`.

Значит его цену надо знать и показывать, а не молча платить: нельзя
ухудшать обязательные показатели ради ограничения, которое мы придумали.

Запуск:
    python experiments/gateway_policy.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.index import ScenarioIndex          # noqa: E402
from backend.core.scenario_store import STORE          # noqa: E402
from backend.planners.base import PlannerConfig        # noqa: E402
from backend.planners.baseline import BaselinePlanner  # noqa: E402
from backend.planners.economic import EconomicPlanner  # noqa: E402
from model.operations import Session                   # noqa: E402

RESULTS = ROOT / "experiments" / "results"
PLANNERS = {"baseline": BaselinePlanner, "economic": EconomicPlanner}


def measure(scenario_id: str, planner_name: str, objective: str,
            protect: bool) -> dict:
    scenario = json.loads(json.dumps(STORE.get(scenario_id).scenario))
    session = Session(scenario)
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(objective=objective, protect_gateway=protect)
    planner = PLANNERS[planner_name]()

    # Что именно гарантирует политика: аппарат, держащий единственный
    # канал, не уходит на калибровку. Калибровка делает аппарат
    # непригодным, работа по downlink — нет (он как раз и передаёт на
    # Землю). Поэтому считаются шаги, где контакт физически был, но все
    # способные аппараты оказались на калибровке, — это и есть потеря
    # связи по нашей вине.
    link_lost = 0
    possible = 0
    calibrated_gateway = 0
    while session.env.k < index.steps:
        step = session.env.k
        contacts = [s for s in index.contacts(step) if session.env.available(s)]
        decisions = planner.decide(session, index, cfg)
        if contacts:
            possible += 1
            busy = [decisions[s].action.get("action") for s in contacts]
            if all(action == "calibrate" for action in busy):
                link_lost += 1
            calibrated_gateway += sum(1 for a in busy if a == "calibrate")
        session.advance({sid: d.action for sid, d in decisions.items()})

    summary = session.summary()
    return {
        "scenario": scenario_id,
        "planner": planner_name,
        "objective": objective,
        "protect_gateway": protect,
        "revenue_usd": summary["revenue_usd"],
        "critical_on_time": summary["critical_jobs_completed_on_time"],
        "critical_due": summary["critical_jobs_due"],
        "jobs_due_missed": summary["jobs_due_missed"],
        "work_steps_in_missed_jobs": summary["work_steps_in_missed_jobs"],
        "steps_with_contact": possible,
        "steps_link_lost_to_calibration": link_lost,
        "contact_satellites_calibrated": calibrated_gateway,
        "link_kept_share": round(1 - link_lost / possible, 4) if possible else None,
    }


def main() -> int:
    rows = [measure(scenario_id, planner, objective, protect)
            for scenario_id in [s["id"] for s in STORE.list()]
            for planner in ("baseline", "economic")
            for objective in ("priority_service", "commercial")
            for protect in (True, False)]

    for row in rows:
        print(f"{row['scenario']:<11} {row['planner']:<9} {row['objective']:<17} "
              f"защита={str(row['protect_gateway']):<5} "
              f"${row['revenue_usd']:>10,.0f}  "
              f"P3 {row['critical_on_time']:>4}/{row['critical_due']:<4} "
              f"просрочено {row['jobs_due_missed']:<5} "
              f"связь потеряна на калибровке "
              f"{row['steps_link_lost_to_calibration']:>3}/"
              f"{row['steps_with_contact']}  "
              f"(калибровок на связи {row['contact_satellites_calibrated']})")

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "gateway_policy.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\nРезультаты: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
