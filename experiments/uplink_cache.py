"""Объём командного трафика и эффект кэша бандлов на Земле.

Земля помнит, что лежит на каждом борту, и передаёт только разницу.
Скрипт считает, сколько ушло бы без кэша и сколько уходит с ним, и
**проверяет главное свойство**: решения борта от кэша не меняются.

Чего замер НЕ показывает — экономии энергии аппарата. Модель кейса
командный канал не моделирует вообще (`env.step` принимает команды
независимо от связи), приём полётного задания не тарифицируется, и
энергию на борту тратит `downlink`/`relay` — оплачиваемая работа.
Объём аплинка это метрика нашего расширения, и выдавать её за экономию
энергии нельзя. Подробности — experiments/results/findings.md.

Запуск:
    python experiments/uplink_cache.py
    python experiments/uplink_cache.py --scenarios P02_shift --gaps 1 4
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.index import ScenarioIndex            # noqa: E402
from backend.core.scenario_store import STORE            # noqa: E402
from backend.planners.base import PlannerConfig          # noqa: E402
from backend.planners.federated import FederatedPlanner  # noqa: E402
from model.operations import Session                     # noqa: E402

RESULTS = ROOT / "experiments" / "results"
MIB = 1048576


def play(scenario_id: str, gap: int, cache: bool) -> tuple[dict, dict, str]:
    scenario = json.loads(json.dumps(STORE.get(scenario_id).scenario))
    session = Session(scenario)
    index = ScenarioIndex.build(session.initial_scenario)
    cfg = PlannerConfig(mode="federated", replan_gap_steps=gap)
    planner = FederatedPlanner(cfg.bundle_horizon_steps, gap, cache_uplink=cache)

    while session.env.k < index.steps:
        decisions = planner.decide(session, index, cfg)
        session.advance({sid: d.action for sid, d in decisions.items()})

    return planner.log.volume(), session.summary(), session.state_digest()


def measure(scenario_id: str, gap: int) -> dict:
    off, summary_off, digest_off = play(scenario_id, gap, False)
    on, summary_on, digest_on = play(scenario_id, gap, True)

    identical = summary_off == summary_on and digest_off == digest_on
    if not identical:
        raise SystemExit(
            f"{scenario_id} gap={gap}: кэш изменил расчёт — это недопустимо, "
            "борт обязан получать тот же бандл")

    return {
        "scenario": scenario_id,
        "replan_gap": gap,
        "uploads": on["uploads"],
        "bytes_without_cache": off["bytes_sent"],
        "bytes_with_cache": on["bytes_sent"],
        "saved_share": round(1 - on["bytes_sent"] / off["bytes_sent"], 4)
        if off["bytes_sent"] else None,
        "items_without_cache": on["items_without_cache"],
        "items_sent": on["items_sent"],
        "items_removed": on["items_removed"],
        "decisions_identical": True,
        "revenue_usd": summary_on["revenue_usd"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", nargs="*",
                        default=["P02_shift", "P03_energy", "P04_demand"])
    parser.add_argument("--gaps", nargs="*", type=int, default=[1, 4, 12, 48])
    args = parser.parse_args()

    rows = [measure(scenario_id, gap)
            for scenario_id in args.scenarios
            for gap in args.gaps]

    print(f"{'сценарий':<12}{'gap':<5}{'загрузок':>9}{'без кэша':>12}"
          f"{'с кэшем':>12}{'экономия':>10}   элементов")
    for row in rows:
        print(f"{row['scenario']:<12}{row['replan_gap']:<5}{row['uploads']:>9}"
              f"{row['bytes_without_cache']/MIB:>9.1f} МиБ"
              f"{row['bytes_with_cache']/MIB:>9.2f} МиБ"
              f"{100*row['saved_share']:>9.1f}%   "
              f"{row['items_sent']:>7} из {row['items_without_cache']}"
              f" (+{row['items_removed']} снято)")

    print("\nРешения борта во всех прогонах совпали с версией без кэша.")
    print("Энергии аппарата это не экономит: приём команд модель не "
          "тарифицирует (см. findings.md).")

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "uplink_cache.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\nРезультаты: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
