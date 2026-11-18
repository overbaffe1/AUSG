"""Теневые цены дефицитных ресурсов.

Идея: ресурсы дефицитны по-разному в разные моменты смены. У каждого есть
текущая цена в долларах, и задание запускается, только если приносит
больше, чем расходует.

lambda_E считается по прогнозу заряда на lookahead шагов вперёд
(`flow/energy.py`), а не по мгновенному запасу: энергия дорога тогда,
когда она связывает НА ГОРИЗОНТЕ, а не когда её мало прямо сейчас.

Цены сглаживаются экспоненциально между шагами (`smoothing`). Без этого
оценка пересчитывается с нуля каждый шаг, прыгает вслед за составом окна
активных заданий, и аппарат бросает начатое ради соседа с чуть большей
плотностью — задание при этом не завершает ни то, ни другое.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.core.index import ScenarioIndex


@dataclass(frozen=True)
class ShadowPrices:
    ground_slot: float          # lambda_G, $ за слот наземного сегмента
    energy_wh: dict[str, float]  # lambda_E(s), $ за Вт*ч
    sat_step: dict[str, float]   # lambda_W(s), $ за шаг аппарата

    def to_dict(self) -> dict:
        return {
            "ground_slot": round(self.ground_slot, 4),
            "energy_wh": {k: round(v, 6) for k, v in self.energy_wh.items()},
            "sat_step": {k: round(v, 4) for k, v in self.sat_step.items()},
        }


def _marginal_yield(values: list[float], capacity: int) -> float:
    """Дуал задачи о рюкзаке: доходность последнего влезающего кандидата.

    Если спрос меньше ёмкости, ресурс не связывает и его цена равна нулю.
    """
    if capacity <= 0:
        return max(values, default=0.0)
    if len(values) <= capacity:
        return 0.0
    return sorted(values, reverse=True)[capacity]


def blend(previous: "ShadowPrices | None", current: ShadowPrices,
          smoothing: float) -> ShadowPrices:
    """Экспоненциальное сглаживание: alpha — доля новой оценки."""
    if previous is None or smoothing >= 1.0:
        return current
    alpha = max(0.0, min(1.0, smoothing))

    def mix(old: float, new: float) -> float:
        return alpha * new + (1.0 - alpha) * old

    return ShadowPrices(
        ground_slot=mix(previous.ground_slot, current.ground_slot),
        energy_wh={k: mix(previous.energy_wh.get(k, v), v)
                   for k, v in current.energy_wh.items()},
        sat_step={k: mix(previous.sat_step.get(k, v), v)
                  for k, v in current.sat_step.items()},
    )


def estimate(index: ScenarioIndex, session, step: int,
             soc_safety_margin_pct: float = 2.0,
             lookahead_steps: int = 12,
             forecast=None,
             previous: "ShadowPrices | None" = None,
             smoothing: float = 1.0) -> ShadowPrices:
    """Оценка цен на текущем шаге по активным заданиям."""
    env = session.env
    jobs = env.jobs
    model = index.model
    horizon = min(index.steps, step + lookahead_steps)

    downlink_yields: list[float] = []
    per_sat_yields: dict[str, list[float]] = {s: [] for s in index.sat_ids}

    for job_id in index.active:
        job = jobs.get(job_id)
        if job is None:
            continue
        remaining = job["remaining_steps"]
        if remaining <= 0:
            continue
        density = float(job["value_usd"]) / remaining
        if job["kind"] == "downlink":
            downlink_yields.append(density)
        for sid in job["eligible_satellites"]:
            if sid in per_sat_yields:
                per_sat_yields[sid].append(density)

    # lambda_G: сколько downlink-слотов остаётся до горизонта.
    slots_left = int(model["downlink_parallel_limit"]) * max(0, horizon - step)
    lambda_g = _marginal_yield(downlink_yields, slots_left)

    # lambda_W(s): шаги конкретного аппарата до горизонта.
    steps_left = max(0, horizon - step)
    lambda_w = {sid: _marginal_yield(vals, steps_left)
                for sid, vals in per_sat_yields.items()}

    # lambda_E(s): энергия стоит денег только когда она связывает, то есть
    # когда запас сверх резерва мал. Цена = доходность шага, который этой
    # энергией можно оплатить.
    lambda_e: dict[str, float] = {}
    reserve_pct = float(model["reserve_soc_pct"]) + soc_safety_margin_pct
    for sid in index.sat_ids:
        state = env.state[sid]
        capacity = index.sats[sid]["capacity_wh"]
        per_step = _worst_case_step_wh(index, sid)

        if forecast is not None:
            # Прогноз уже учёл солнце, нагреватель и КПД на горизонте:
            # сколько рабочих шагов аппарат реально может оплатить.
            steps_affordable = float(min(forecast.energy_steps_for(sid, "downlink"),
                                         forecast.energy_steps_for(sid, "relay")))
        else:
            headroom = state["energy_wh"] - capacity * reserve_pct / 100.0
            steps_affordable = (headroom / per_step if per_step > 0 and headroom > 0
                                else 0.0)

        if steps_affordable >= steps_left or per_step <= 0:
            # Энергии хватает на весь горизонт — ресурс не связывает.
            lambda_e[sid] = 0.0
        else:
            # Цена = доходность шага, который этой энергией оплачивается.
            lambda_e[sid] = lambda_w[sid] / per_step

    current = ShadowPrices(ground_slot=lambda_g, energy_wh=lambda_e, sat_step=lambda_w)
    return blend(previous, current, smoothing)


def _worst_case_step_wh(index: ScenarioIndex, sid: str) -> float:
    sat = index.sats[sid]
    power = max(float(sat["downlink_w"]), float(sat["relay_w"]))
    return power * index.step_s / 3600.0 / index.model["discharge_efficiency"]
