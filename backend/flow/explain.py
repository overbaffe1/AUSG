"""Разбор конкретного решения.

Требование постановки: пользователь должен разобрать решение — какие
сведения были доступны, какое ограничение повлияло на выбор и каковы
последствия. Причины простоя, отказа в операции и срыва задания не
смешиваются.

Отдельно: текущая калибровка объясняет отсутствие передачи на этом шаге,
но сама по себе не доказывает, что задание нельзя было завершить до
срока. Поэтому вывод о невыполнимости строится не на причине отказа,
а на арифметике окон (см. flow/valuation.opportunities).
"""
from __future__ import annotations

from backend.planners.base import REASONS


def decision_report(run, step: int, satellite_id: str,
                    full: bool = False) -> dict:
    """Разбор одного решения.

    По умолчанию отдаётся то, что хранится: выбранное действие, причина,
    теневые цены и рассмотренные кандидаты. Этого хватает панели разбора,
    и ответ мгновенный на любом шаге.

    Глубокая часть — список отклонённых с причинами и карта стоимости —
    у старых шагов не хранится ради памяти (З6) и восстанавливается
    повтором прогона. На P04_demand это до пяти секунд, поэтому она
    запрашивается отдельно (``full=true``), а не при каждом открытии
    панели: критерий О3 требует, чтобы разбор открывался за 300 мс.
    """
    decisions = run.decisions.get(step)
    if not decisions or satellite_id not in decisions:
        return {"step": step, "satellite_id": satellite_id,
                "available": False,
                "message": "Решение для этого шага ещё не принято"}

    decision = decisions[satellite_id]
    truncated = bool(decision.get("explain", {}).get("truncated"))
    if truncated and full:
        restored = run.recompute_decision(step)
        if restored and satellite_id in restored:
            decision = restored[satellite_id]
            decision.setdefault("explain", {})["recomputed"] = True
            truncated = False
    row = next((r for r in run.session.env.trace
                if r["step"] == step and r["satellite_id"] == satellite_id), None)

    report = {
        "step": step,
        "satellite_id": satellite_id,
        "requested": decision["action"],
        "reason_code": decision["reason_code"],
        "reason_text": REASONS.get(decision["reason_code"], decision["reason_code"]),
        "score": decision["score"],
        "considered": decision["explain"],
        # Глубокая часть отсутствует и доступна по запросу full=true.
        "detail_available": not truncated,
    }
    if row is not None:
        report["executed"] = row["executed"]
        report["model_reason"] = row["reason"]
        report["consequences"] = {
            "energy_before_wh": row["energy_before_wh"],
            "energy_after_wh": row["energy_after_wh"],
            "temp_before_c": row["temp_before_c"],
            "temp_after_c": row["temp_after_c"],
            "load_w": row["load_w"],
            "heater_w": row["heater_w"],
            "calibration_age_steps": row["calibration_age_steps"],
            "completed_job": row["completed_job"],
            "below_reserve": row["below_reserve"],
            "brownout": row["brownout"],
        }
        report["mismatch"] = (row["executed"] != decision["action"].get("action"))
    return report


def idle_breakdown(run) -> dict[str, int]:
    """Распределение причин простоя по всей исполненной истории."""
    counts: dict[str, int] = {}
    for step_decisions in run.decisions.values():
        for decision in step_decisions.values():
            if decision["action"].get("action") != "idle":
                continue
            code = decision["reason_code"]
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
