"""Основной планировщик: теневые цены + допуск в работу.

Конвейер на каждом шаге:
    1. обновить окно активных заданий;
    2. оценить теневые цены ресурсов;
    3. выбрать шлюз и защитить его от калибровки;
    4. зарезервировать слоты наземного сегмента под лучшие downlink;
    5. назначить relay жадно по чистой ценности;
    6. поставить калибровку тем, кому она нужна и кто свободен;
    7. проверить собственный план через can_execute.

ЭТАП 2 (Dev A): пункт 4-5 сейчас жадные. При запасе времени заменить на
венгерский алгоритм на двудольном графе «аппараты x задания».
"""
from __future__ import annotations

from typing import Any

from backend.core.index import ScenarioIndex
from backend.flow import admission, dispatch, energy, shadow_prices
from backend.planners.base import (CALIBRATE, IDLE, Decision, PlannerConfig,
                                   job_action)
from backend.planners.baseline import select_gateway


class EconomicPlanner:
    name = "economic"
    version = "0.2"

    def __init__(self) -> None:
        # Сглаживание цен (З3) и гистерезис вытеснения (З2) требуют памяти
        # между шагами, поэтому планировщик хранится в Run, а не создаётся
        # заново (см. core/registry.py).
        self._prices = None
        self._holding: dict[str, tuple[str, int]] = {}

    def decide(self, session, index: ScenarioIndex,
               cfg: PlannerConfig) -> dict[str, Decision]:
        env = session.env
        step = env.k
        model = index.model
        index.refresh(step, env.jobs)

        forecast = energy.build(index, session, step, cfg.lookahead_steps)
        prices = shadow_prices.estimate(index, session, step,
                                        cfg.soc_safety_margin_pct,
                                        cfg.lookahead_steps,
                                        forecast=forecast,
                                        previous=self._prices,
                                        smoothing=cfg.price_smoothing)
        self._prices = prices
        gateway = select_gateway(session, index, step) if cfg.protect_gateway else None

        # --- 1. собрать допущенные пары -------------------------------------
        verdicts: list[admission.Verdict] = []
        rejected: dict[str, list[dict]] = {}
        for sid in index.sat_ids:
            if not env.available(sid):
                continue
            for job in index.candidates_for(sid, env.jobs):
                if not (job["release_step"] <= step < job["deadline_step"]):
                    continue
                if not index.available(sid, job["kind"], step):
                    continue
                verdict = admission.evaluate(
                    index, session, sid, job, step, prices, cfg.objective,
                    gamma=cfg.urgency_gamma, critical_weight=cfg.critical_weight,
                    tightness_limit=cfg.admission_tightness_limit,
                    forecast=forecast)
                if verdict.admitted:
                    verdicts.append(verdict)
                else:
                    rejected.setdefault(sid, []).append(verdict.to_dict())

        # Лексикографический порядок: сначала разряд режима, потом деньги.
        verdicts.sort(key=lambda v: v.sort_key)

        # --- 2. распределить с учётом глобальных ограничений -----------------
        downlink_budget = int(model["downlink_parallel_limit"])
        taken_jobs: set[str] = set()
        assigned: dict[str, admission.Verdict] = {}
        shortlist: dict[str, list[dict]] = {}
        preempted: dict[str, dict] = {}

        for verdict in verdicts:
            sid = verdict.satellite_id
            shortlist.setdefault(sid, [])
            if len(shortlist[sid]) < 5:
                shortlist[sid].append({"job_id": verdict.job_id,
                                       "net_value": round(verdict.net_value, 4)})

        def take(verdict: admission.Verdict) -> bool:
            """Закрепить пару, если модель это разрешает и слот есть."""
            nonlocal downlink_budget
            sid, job_id = verdict.satellite_id, verdict.job_id
            if sid in assigned or job_id in taken_jobs:
                return False
            downlink = env.jobs[job_id]["kind"] == "downlink"
            if downlink and downlink_budget <= 0:
                return False
            ok, reason, _ = env.can_execute(sid, job_action(job_id))
            if not ok:
                rejected.setdefault(sid, []).append(
                    {"job_id": job_id, "reason": reason, "admitted": False})
                return False
            assigned[sid] = verdict
            taken_jobs.add(job_id)
            if downlink:
                downlink_budget -= 1
            return True

        # 2а. Право первой руки — у начатого задания (З2).
        #
        # Без этого планировщик каждый шаг пересобирает назначение заново и
        # бросает начатое ради соседа с чуть большей плотностью: ресурсы
        # потрачены, выручки нет. Вытеснение допускается, но обязано быть
        # обоснованным: выигрыш должен перекрыть уже вложенные шаги
        # (`admission.preemption_gain`) и порог безразличия.
        #
        # Асимметрия из данных: downlink-задание закреплено за единственным
        # допустимым исполнителем (проверено тестом), поэтому переезжать ему
        # некуда. relay может подхватить другой аппарат с сохранением
        # прогресса — прогресс модель ведёт на задании, а не на аппарате.
        by_pair = {(v.satellite_id, v.job_id): v for v in verdicts}
        preempt_checked: set[str] = set()

        for verdict in verdicts:
            sid = verdict.satellite_id
            held = self._holding.get(sid)

            if (held is not None and sid not in assigned
                    and sid not in preempt_checked
                    and held[0] != verdict.job_id):
                preempt_checked.add(sid)
                incumbent = by_pair.get((sid, held[0]))
                job = env.jobs.get(held[0])
                if incumbent is not None and job is not None                         and job["completed_step"] is None:
                    done = int(job["work_steps"]) - int(job["remaining_steps"])
                    hysteresis = (cfg.preemption_hysteresis_frac
                                  * float(job["value_usd"]))
                    gain = admission.preemption_gain(
                        incumbent, verdict, done,
                        prices.sat_step.get(sid, 0.0),
                        hysteresis=hysteresis)
                    # Выдержка: задание, начатое только что, не вытесняется —
                    # иначе два почти равных претендента меняются местами на
                    # соседних шагах и не завершается ни один.
                    held_enough = held[1] >= cfg.preemption_min_hold_steps
                    if gain > 0 and held_enough:
                        if take(verdict):
                            preempted[sid] = {"job_id": held[0],
                                              "reason": "preempted_by",
                                              "by_job_id": verdict.job_id,
                                              "gain": round(gain, 4),
                                              "work_done_steps": done}
                            rejected.setdefault(sid, []).insert(0, preempted[sid])
                        continue
                    if take(incumbent):
                        continue

            take(verdict)

        # 2в. Остаток — венгерским алгоритмом, если он включён (З4).
        #
        # Жадность теряет там, где два аппарата конкурируют за одно дорогое
        # задание, имея разные альтернативы. Здесь это доигрывается на
        # двудольном графе с максимизацией суммы NV.
        if cfg.assignment == "optimal":
            rest = [v for v in verdicts
                    if v.satellite_id not in assigned and v.job_id not in taken_jobs]
            if rest:
                plan = dispatch.assign_optimal(
                    rest, downlink_budget,
                    kind_of=lambda job_id: env.jobs[job_id]["kind"],
                    top_per_sat=cfg.assignment_top_per_sat)
                for verdict in sorted(plan.values(), key=lambda v: v.sort_key):
                    take(verdict)

        # --- 3. собрать решения ----------------------------------------------
        decisions: dict[str, Decision] = {}
        valid = int(model["calibration_valid_steps"])

        for sid in index.sat_ids:
            explain: dict[str, Any] = {
                "step": step,
                "is_gateway": sid == gateway,
                "shadow_prices": {
                    "ground_slot": round(prices.ground_slot, 4),
                    "energy_wh": round(prices.energy_wh.get(sid, 0.0), 6),
                    "sat_step": round(prices.sat_step.get(sid, 0.0), 4),
                },
                "top_candidates": shortlist.get(sid, []),
                "rejected": rejected.get(sid, [])[:5],
            }
            if sid in preempted:
                explain["preempted"] = preempted[sid]

            if not env.available(sid):
                decisions[sid] = Decision(IDLE, 0.0, "satellite_unavailable", explain)
                continue

            verdict = assigned.get(sid)
            if verdict is not None:
                explain["chosen_job"] = verdict.job_id
                explain["net_value"] = round(verdict.net_value, 4)
                explain["detail"] = verdict.detail
                decisions[sid] = Decision(job_action(verdict.job_id),
                                          verdict.net_value, "accepted", explain)
                continue

            age = env.state[sid]["calibration_age_steps"]
            if sid == gateway:
                reason = "gateway_protected" if age >= valid - cfg.calibration_margin_steps \
                    else "gateway_hold"
                decisions[sid] = Decision(IDLE, 0.0, reason, explain)
                continue

            if age >= valid - cfg.calibration_margin_steps:
                ok, reason, _ = env.can_execute(sid, CALIBRATE)
                if ok:
                    decisions[sid] = Decision(CALIBRATE, 0.0, "calibration_required",
                                              explain)
                    continue
                explain["calibration_blocked_by"] = reason

            if age > valid // 2:
                ok, _, _ = env.can_execute(sid, CALIBRATE)
                if ok:
                    decisions[sid] = Decision(CALIBRATE, 0.0, "calibration_preventive",
                                              explain)
                    continue

            reason = _idle_reason(explain)
            decisions[sid] = Decision(IDLE, 0.0, reason, explain)

        self._remember_holding(decisions, env)
        return decisions

    def _remember_holding(self, decisions: dict[str, Decision], env) -> None:
        """Запомнить, кто какое задание ведёт и сколько шагов подряд."""
        holding: dict[str, tuple[str, int]] = {}
        for sid, decision in decisions.items():
            job_id = decision.action.get("job_id")
            if decision.action.get("action") != "job" or job_id is None:
                continue
            previous = self._holding.get(sid)
            held = previous[1] + 1 if previous and previous[0] == job_id else 1
            holding[sid] = (job_id, held)
        self._holding = holding


def _idle_reason(explain: dict) -> str:
    """Одна конкретная причина простоя, без смешивания."""
    codes = {r.get("reason") for r in explain.get("rejected", [])}
    mapping = {
        "energy_reserve": "energy_recovery",
        "thermal_limit": "thermal_hold",
        "infeasible_by_contacts": "infeasible_by_contacts",
        "infeasible_by_deadline": "infeasible_by_deadline",
        "negative_value": "negative_value",
        "no_completion_margin": "no_completion_margin",
        "energy_forecast_short": "energy_forecast_short",
        "calibration_required": "calibration_required",
    }
    for code, reason in mapping.items():
        if code in codes:
            return reason
    return "no_eligible_job"
