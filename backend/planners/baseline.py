"""Простое понятное правило — обязательная база для сравнения.

Постановка требует реализовать собственное простое правило и проверить его
на тех же условиях, что и основной подход. Здесь три варианта выбора:

    edf      — ближайший срок;
    value    — наибольшая стоимость;
    priority — сначала приоритет 3, далее ближайший срок.

Планировщик уже уважает глобальные ограничения: лимит наземного сегмента,
один исполнитель на задание, защита шлюза. Это делает его честным
соперником, а не соломенным чучелом.
"""
from __future__ import annotations

from typing import Any

from backend.core.index import ScenarioIndex
from backend.planners.base import (CALIBRATE, IDLE, Decision, PlannerConfig,
                                   job_action)


def _sort_key(rule: str):
    if rule == "value":
        return lambda j: (-float(j["value_usd"]), j["deadline_step"], j["id"])
    if rule == "priority":
        return lambda j: (-int(j["priority"]), j["deadline_step"],
                          -float(j["value_usd"]), j["id"])
    return lambda j: (j["deadline_step"], -float(j["value_usd"]), j["id"])


def select_gateway(session, index: ScenarioIndex, step: int) -> str | None:
    """Кто держит связь с Землёй на этом шаге.

    Требование кейсодержателя: хотя бы один аппарат должен оставаться
    подключённым. В выданных данных почти в половине шагов кандидат ровно
    один, поэтому отправить его на калибровку значит потерять связь.

    Выбирается аппарат с наименьшим числом оставшихся окон контакта —
    его окна дефицитнее всего. Если кандидатов нет, связи нет физически
    (в P02/P03/P04 это шаги 224 и 230) — возвращается None.
    """
    contacts = [s for s in index.contacts(step) if session.env.available(s)]
    if not contacts:
        return None
    return min(contacts,
               key=lambda s: (index.remaining_windows(s, "downlink", step, index.steps), s))


class BaselinePlanner:
    name = "baseline"
    version = "1.0"

    def decide(self, session, index: ScenarioIndex,
               cfg: PlannerConfig) -> dict[str, Decision]:
        env = session.env
        step = env.k
        model = index.model
        index.refresh(step, env.jobs)

        gateway = select_gateway(session, index, step) if cfg.protect_gateway else None
        key = _sort_key(cfg.rule)

        downlink_budget = int(model["downlink_parallel_limit"])
        taken_jobs: set[str] = set()
        decisions: dict[str, Decision] = {}

        for sid in index.sat_ids:
            state = env.state[sid]
            explain: dict[str, Any] = {"step": step, "is_gateway": sid == gateway}

            if not env.available(sid):
                decisions[sid] = Decision(IDLE, 0.0, "satellite_unavailable", explain)
                continue

            age = state["calibration_age_steps"]
            valid = int(model["calibration_valid_steps"])
            candidates = sorted(
                (j for j in index.candidates_for(sid, env.jobs)
                 if j["release_step"] <= step < j["deadline_step"]
                 and j["id"] not in taken_jobs
                 and index.available(sid, j["kind"], step)),
                key=key,
            )
            explain["candidates_considered"] = len(candidates)

            # --- калибровка -------------------------------------------------
            expiring = age >= valid - cfg.calibration_margin_steps
            if expiring and candidates:
                if sid == gateway:
                    # Связь важнее: калибровку откладываем, чтобы не остаться
                    # без единственного канала.
                    explain["deferred"] = "calibration"
                    decisions[sid] = Decision(IDLE, 0.0, "gateway_protected", explain)
                    continue
                ok, reason, _ = env.can_execute(sid, CALIBRATE)
                if ok:
                    decisions[sid] = Decision(CALIBRATE, 0.0, "calibration_required",
                                              explain)
                    continue
                explain["calibration_blocked_by"] = reason

            # --- задание ----------------------------------------------------
            chosen: Decision | None = None
            for job in candidates:
                if job["kind"] == "downlink" and downlink_budget <= 0:
                    explain["blocked_by"] = "ground_capacity_reserved"
                    continue
                action = job_action(job["id"])
                ok, reason, _ = env.can_execute(sid, action)
                if not ok:
                    explain.setdefault("rejected", []).append(
                        {"job_id": job["id"], "reason": reason})
                    continue
                explain["chosen_job"] = job["id"]
                explain["chosen_deadline"] = job["deadline_step"]
                explain["chosen_value_usd"] = job["value_usd"]
                chosen = Decision(action, float(job["value_usd"]), "accepted", explain)
                taken_jobs.add(job["id"])
                if job["kind"] == "downlink":
                    downlink_budget -= 1
                break

            if chosen is not None:
                decisions[sid] = chosen
                continue

            # --- простой ----------------------------------------------------
            if sid == gateway:
                decisions[sid] = Decision(IDLE, 0.0, "gateway_hold", explain)
                continue
            if not candidates:
                # Профилактика в простое: калибровка дешевле передачи
                # (20 Вт против 90), а просроченный аппарат бесполезен.
                if age > valid // 2:
                    ok, _, _ = env.can_execute(sid, CALIBRATE)
                    if ok:
                        decisions[sid] = Decision(CALIBRATE, 0.0,
                                                  "calibration_preventive", explain)
                        continue
                decisions[sid] = Decision(IDLE, 0.0, "no_eligible_job", explain)
                continue

            reason = explain.get("blocked_by") or _idle_reason(explain, index, sid,
                                                              state, model)
            decisions[sid] = Decision(IDLE, 0.0, reason, explain)

        return decisions


def _idle_reason(explain: dict, index: ScenarioIndex, sid: str,
                 state: dict, model: dict) -> str:
    """Одна конкретная причина простоя. Причины не смешиваются."""
    reasons = {r["reason"] for r in explain.get("rejected", [])}
    for code in ("energy_reserve", "thermal_limit", "calibration_required",
                 "no_contact", "outside_job_window"):
        if code in reasons:
            return {"energy_reserve": "energy_recovery",
                    "thermal_limit": "thermal_hold"}.get(code, code)
    return "no_eligible_job"
