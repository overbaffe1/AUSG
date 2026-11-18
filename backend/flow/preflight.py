"""Предполётная оценка смены — до первого шага расчёта.

Отвечает на вопрос оператора «что вообще возможно», прежде чем он
запустит расчёт: где дефицит, что невыполнимо и почему, сколько денег
на столе. На P04_demand сразу видно, что спрос превышает ёмкость
группировки, то есть часть заданий будет отброшена при любой стратегии.
"""
from __future__ import annotations

from backend.core.index import ScenarioIndex
from backend.flow import valuation


def analyse(scenario: dict) -> dict:
    index = ScenarioIndex.build(scenario)
    n = index.steps
    jobs = scenario["jobs"]
    model = index.model

    # ------------------------------------------------------------ связь
    contacts_per_step = [len(index.contacts(k)) for k in range(n)]
    single_gateway = [k for k in range(n) if contacts_per_step[k] == 1]
    blind = [k for k in range(n)
             if contacts_per_step[k] == 0
             and not any(index.available(s, "relay", k) for s in index.sat_ids)]
    longest_blind = current = 0
    for k in range(n):
        current = current + 1 if contacts_per_step[k] == 0 else 0
        longest_blind = max(longest_blind, current)

    # ------------------------------------------------------------ ёмкость
    downlink_jobs = [j for j in jobs if j["kind"] == "downlink"]
    relay_jobs = [j for j in jobs if j["kind"] == "relay"]
    downlink_demand = sum(j["work_steps"] for j in downlink_jobs)
    relay_demand = sum(j["work_steps"] for j in relay_jobs)
    limit = int(model["downlink_parallel_limit"])

    # Ёмкость считается по расписанию контактов, а не теоретическим потолком
    # (правка внешнего аудита, З8.1). Потолок `limit * steps` предполагает,
    # что на каждом шаге есть два аппарата на связи, — в выданных данных
    # это неверно: слотов 439 из 576, а на шагах 224 и 230 нет ни одного.
    downlink_ceiling = limit * n
    downlink_capacity = sum(min(limit, contacts_per_step[k]) for k in range(n))

    # Для relay то же самое: аппарат без relay-окна ёмкости не даёт.
    relay_ceiling = len(index.sat_ids) * n
    sat_capacity = sum(1 for k in range(n) for s in index.sat_ids
                       if index.available(s, "relay", k))

    # ------------------------------------------------------------ невыполнимое
    infeasible: list[dict] = []
    for job in jobs:
        windows = valuation.opportunities(index, job, job["release_step"])
        if windows < job["work_steps"]:
            infeasible.append({
                "job_id": job["id"], "kind": job["kind"],
                "priority": job["priority"], "value_usd": job["value_usd"],
                "work_steps": job["work_steps"], "opportunities": windows,
                "reason": "infeasible_by_contacts" if windows else "infeasible_by_deadline",
                "eligible_satellites": job["eligible_satellites"],
            })

    # аппараты, у которых собственный downlink-спрос больше их окна связи
    starved = []
    for sid in index.sat_ids:
        demand = sum(j["work_steps"] for j in downlink_jobs
                     if j["eligible_satellites"] == [sid])
        window = index.remaining_windows(sid, "downlink", 0, n)
        if demand > window:
            starved.append({"satellite_id": sid, "demand_steps": demand,
                            "contact_steps": window})

    # ------------------------------------------------------------ энергия
    dt = index.step_s
    energy_short = []
    for sid in index.sat_ids:
        sat = index.sats[sid]
        solar = sum(index.env[sid]["solar_w"]) * dt / 3600
        base = sat["base_w"] * n * dt / 3600
        budget = (sat["capacity_wh"] * (sat["initial_soc_pct"] - model["reserve_soc_pct"]) / 100
                  + solar * model["charge_efficiency"]
                  - base / model["discharge_efficiency"])
        demand = sum(
            sat[j["kind"] + "_w"] * j["work_steps"] * dt / 3600
            / model["discharge_efficiency"] / len(j["eligible_satellites"])
            for j in jobs if sid in j["eligible_satellites"])
        if demand > budget:
            energy_short.append({"satellite_id": sid,
                                 "budget_wh": round(budget, 1),
                                 "demand_wh": round(demand, 1)})

    infeasible_value = sum(j["value_usd"] for j in infeasible)
    total_value = sum(j["value_usd"] for j in jobs)

    return {
        "scenario_id": scenario["meta"]["id"],
        "steps": n,
        "satellites": len(index.sat_ids),
        "jobs_total": len(jobs),
        "ground_link": {
            "steps_with_single_gateway": len(single_gateway),
            "single_gateway_share": round(len(single_gateway) / n, 4),
            "blind_steps": blind,
            "longest_gap_steps": longest_blind,
            "contacts_histogram": {str(v): contacts_per_step.count(v)
                                   for v in sorted(set(contacts_per_step))},
        },
        "capacity": {
            # capacity_steps и oversubscription считаются ПО ОКНАМ СВЯЗИ —
            # это число, по которому принимается решение. Теоретический
            # потолок отдаётся рядом, чтобы разрыв между ними был виден.
            "downlink": {"demand_steps": downlink_demand,
                         "capacity_steps": downlink_capacity,
                         "contact_capacity": downlink_capacity,
                         "theoretical_ceiling": downlink_ceiling,
                         "oversubscription": round(downlink_demand / downlink_capacity, 4)
                         if downlink_capacity else None,
                         "oversubscription_theoretical":
                             round(downlink_demand / downlink_ceiling, 4)
                             if downlink_ceiling else None},
            "relay": {"demand_steps": relay_demand,
                      "capacity_steps": sat_capacity,
                      "contact_capacity": sat_capacity,
                      "theoretical_ceiling": relay_ceiling,
                      "oversubscription": round(relay_demand / sat_capacity, 4)
                      if sat_capacity else None,
                      "oversubscription_theoretical":
                          round(relay_demand / relay_ceiling, 4)
                          if relay_ceiling else None},
        },
        "structural_infeasible": {
            "count": len(infeasible),
            "value_usd": round(infeasible_value, 2),
            "jobs": infeasible[:200],
            "truncated": len(infeasible) > 200,
        },
        "starved_satellites": starved,
        "energy_short_satellites": energy_short,
        "money": {
            "total_usd": round(total_value, 2),
            "priority3_usd": round(sum(j["value_usd"] for j in jobs
                                       if j["priority"] == 3), 2),
            "priority3_jobs": sum(1 for j in jobs if j["priority"] == 3),
            "reachable_ceiling_usd": round(total_value - infeasible_value, 2),
        },
    }
