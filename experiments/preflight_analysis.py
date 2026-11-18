"""Предполётный анализ сценариев: связь, дефицит ресурсов, деньги на столе.

Запуск:  python experiments/preflight_analysis.py
Цифры из этого скрипта использованы в разделе 2 ТЗ.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ("P01_intro", "P02_shift", "P03_energy", "P04_demand")


def analyse(name: str) -> dict:
    s = json.loads((ROOT / "data" / f"{name}.json").read_text(encoding="utf-8"))
    n = s["time"]["steps"]
    dt = s["time"]["step_s"]
    m = s["model"]
    env = s["environment"]
    sats = {v["id"]: v for v in s["satellites"]}

    # --- связь: сколько аппаратов одновременно и как долго каждый ---
    dl_per_step = [sum(env[i]["downlink_available"][k] for i in sats) for k in range(n)]
    dl_per_sat = [sum(env[i]["downlink_available"]) for i in sats]
    rl_per_sat = [sum(env[i]["relay_available"]) for i in sats]

    # --- шлюз: шаги с единственным каналом и шаги полной слепоты ---
    single_gateway = [k for k in range(n) if dl_per_step[k] == 1]
    blind = [k for k in range(n)
             if dl_per_step[k] == 0
             and not any(env[i]["relay_available"][k] for i in sats)]
    longest_blind = cur = 0
    for k in range(n):
        cur = cur + 1 if dl_per_step[k] == 0 else 0
        longest_blind = max(longest_blind, cur)

    # --- ёмкость против спроса ---
    dl_jobs = [j for j in s["jobs"] if j["kind"] == "downlink"]
    rl_jobs = [j for j in s["jobs"] if j["kind"] == "relay"]
    dl_demand = sum(j["work_steps"] for j in dl_jobs)
    rl_demand = sum(j["work_steps"] for j in rl_jobs)
    dl_capacity = m["downlink_parallel_limit"] * n
    sat_capacity = len(sats) * n

    # downlink закреплён за одним аппаратом: спрос против ЕГО окна связи
    starved = [
        i for i in sats
        if sum(j["work_steps"] for j in dl_jobs if j["eligible_satellites"] == [i])
        > sum(env[i]["downlink_available"])
    ]

    # --- энергобаланс: бюджет против доли спроса ---
    short = []
    for i, v in sats.items():
        solar = sum(env[i]["solar_w"]) * dt / 3600
        base = v["base_w"] * n * dt / 3600
        budget = (v["capacity_wh"] * (v["initial_soc_pct"] - m["reserve_soc_pct"]) / 100
                  + solar * m["charge_efficiency"]
                  - base / m["discharge_efficiency"])
        demand = sum(
            v[j["kind"] + "_w"] * j["work_steps"] * dt / 3600
            / m["discharge_efficiency"] / len(j["eligible_satellites"])
            for j in s["jobs"] if i in j["eligible_satellites"]
        )
        if demand > budget:
            short.append(i)

    return {
        "name": name, "steps": n, "sats": len(sats), "jobs": len(s["jobs"]),
        "single_gateway": single_gateway, "blind": blind, "longest_blind": longest_blind,
        "dl_concurrent": (min(dl_per_step), statistics.median(dl_per_step), max(dl_per_step)),
        "dl_window": (min(dl_per_sat), statistics.median(dl_per_sat), max(dl_per_sat)),
        "rl_window": (min(rl_per_sat), statistics.median(rl_per_sat), max(rl_per_sat)),
        "dl_demand": dl_demand, "dl_capacity": dl_capacity,
        "rl_demand": rl_demand, "sat_capacity": sat_capacity,
        "starved": starved, "energy_short": short,
        "value_total": sum(j["value_usd"] for j in s["jobs"]),
        "value_p3": sum(j["value_usd"] for j in s["jobs"] if j["priority"] == 3),
        "jobs_p3": sum(1 for j in s["jobs"] if j["priority"] == 3),
    }


def report(r: dict) -> None:
    print(f"\n=== {r['name']}  —  {r['sats']} КА, {r['steps']} шагов, {r['jobs']} заданий")
    print("  Связь с Землёй:")
    print("    одновременно в контакте: min/медиана/max = {}/{:.0f}/{}".format(*r["dl_concurrent"]))
    print("    окно на аппарат из {} шагов: min/медиана/max = {}/{:.0f}/{}".format(
        r["steps"], *r["dl_window"]))
    print("    relay доступен на аппарат: медиана {:.0f} из {} шагов".format(
        r["rl_window"][1], r["steps"]))
    print("  Шлюз (связь держится на одном аппарате):")
    print("    шагов с ЕДИНСТВЕННЫМ аппаратом в контакте: {} из {} ({:.0f}%)"
          "  <-- калибровать его нельзя".format(
              len(r["single_gateway"]), r["steps"],
              100 * len(r["single_gateway"]) / r["steps"]))
    print("    шагов полной слепоты (нет ни downlink, ни relay): {}{}".format(
        len(r["blind"]),
        "  шаги " + str(r["blind"]) if 0 < len(r["blind"]) <= 6 else ""))
    print("    максимальный разрыв связи с Землёй подряд: {} шагов".format(r["longest_blind"]))
    print("  Ёмкость против спроса:")
    print("    downlink: {} шаго-слотов спроса из {} ёмкости ({:.0f}%)".format(
        r["dl_demand"], r["dl_capacity"], 100 * r["dl_demand"] / r["dl_capacity"]))
    print("    relay:    {} шаго-слотов спроса из {} ёмкости ({:.0f}%){}".format(
        r["rl_demand"], r["sat_capacity"], 100 * r["rl_demand"] / r["sat_capacity"],
        "  <-- ПЕРЕПОДПИСКА" if r["rl_demand"] > r["sat_capacity"] else ""))
    print("  Структурная невыполнимость:")
    print("    аппаратов, где downlink-спрос больше собственного окна связи: {} из {}".format(
        len(r["starved"]), r["sats"]))
    print("    аппаратов в энергодефиците: {} из {}".format(len(r["energy_short"]), r["sats"]))
    print("  Деньги на столе: ${:,.0f} всего, ${:,.0f} в приоритете 3 ({} заданий)".format(
        r["value_total"], r["value_p3"], r["jobs_p3"]))


def main() -> int:
    for name in SCENARIOS:
        report(analyse(name))
    print("\nВывод: спрос превышает возможности группировки, часть заданий невыполнима")
    print("до запуска расчёта. Задача — выбрать, на чём заработать.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
