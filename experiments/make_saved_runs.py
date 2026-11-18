"""Четыре сохранённых расчёта для сдачи (З7).

  1. обычная смена            — P02_shift, коммерческая отдача;
  2. дефицит энергии          — P03_energy, приоритетное обслуживание;
  3. перегрузка               — P04_demand, коммерческая отдача;
  4. продолжение после событий — P02_shift плюс examples/events_demo.json.

Каждый файл обязан проходить штатную проверку библиотеки:

    python model/operations.py --result examples/saved_runs/<файл>.json \\
        --output runs/replay.json

Скрипт проверяет это сам через ``replay_episode`` и падает, если расчёт
не воспроизводится: выгрузка, которая не сходится, стоит баллов по Т1.

Запуск:
    python experiments/make_saved_runs.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.registry import REGISTRY                # noqa: E402
from backend.planners.base import PlannerConfig           # noqa: E402
from model.operations import replay_episode               # noqa: E402

SAVED = ROOT / "examples" / "saved_runs"
EVENTS = ROOT / "examples" / "events_demo.json"


def load_events() -> list[dict]:
    payload = json.loads(EVENTS.read_text(encoding="utf-8-sig"))
    return payload["events"] if isinstance(payload, dict) else payload


def produce(name: str, scenario_id: str, objective: str, label: str, *,
            mode: str = "centralized", with_events: bool = False) -> dict:
    cfg = PlannerConfig(objective=objective, mode=mode)
    run = REGISTRY.create(scenario_id, cfg, "economic", label=label)

    events = load_events() if with_events else []
    by_step: dict[int, list[dict]] = {}
    for event in events:
        by_step.setdefault(int(event["at_step"]), []).append(event)

    applied = 0
    while not run.finished:
        for event in by_step.get(run.step, ()):
            run.apply_event(event)
            applied += 1
        run.advance(1)

    run.session.run_metadata = run.run_metadata()
    result = run.session.result()

    # Штатная проверка: расчёт обязан воспроизводиться теми же командами.
    replayed = replay_episode(result["initial_scenario"], result["events"],
                              result["commands"], result["steps_executed"])
    mismatches = {k: (v, replayed.summary().get(k))
                  for k, v in result["summary"].items()
                  if replayed.summary().get(k) != v}
    if mismatches:
        raise SystemExit(f"{name}: расчёт не воспроизводится — {mismatches}")

    SAVED.mkdir(parents=True, exist_ok=True)
    path = SAVED / f"{name}.json"
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    REGISTRY.drop(run.id)

    summary = result["summary"]
    return {
        "file": path.name,
        "label": label,
        "scenario": scenario_id,
        "objective": objective,
        "mode": mode,
        "events_applied": applied,
        "size_mb": round(path.stat().st_size / 1048576, 2),
        "revenue_usd": summary["revenue_usd"],
        "critical_on_time": summary["critical_jobs_completed_on_time"],
        "critical_due": summary["critical_jobs_due"],
        "jobs_due_missed": summary["jobs_due_missed"],
        "reproduced": True,
    }


def main() -> int:
    plan = [
        ("normal_shift", "P02_shift", "commercial", "Обычная смена"),
        ("energy_deficit", "P03_energy", "priority_service", "Дефицит энергии"),
        ("overload", "P04_demand", "commercial", "Перегрузка спросом"),
    ]
    rows = [produce(*item) for item in plan]
    rows.append(produce("after_events", "P02_shift", "commercial",
                        "Продолжение после событий", with_events=True))

    for row in rows:
        print(f"{row['file']:<22} {row['label']:<26} {row['scenario']:<11} "
              f"${row['revenue_usd']:>10,.0f}  "
              f"P3 {row['critical_on_time']}/{row['critical_due']}  "
              f"событий {row['events_applied']}  {row['size_mb']} МБ  "
              f"воспроизводится")

    index_path = SAVED / "index.json"
    index_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"\nОписание: {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
