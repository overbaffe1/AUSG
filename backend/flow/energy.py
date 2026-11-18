"""Прогноз заряда и температуры на несколько шагов вперёд.

Зачем отдельный модуль: два разных потребителя просят одно и то же.

  - допуск (`flow/admission.py`) обязан ответить «хватит ли энергии, чтобы
    довести задание до конца». Частичная работа не оплачивается, поэтому
    начинать то, на что не хватит заряда, — прямой убыток;
  - теневая цена энергии (`flow/shadow_prices.py`) должна знать, связывает
    ли ресурс на горизонте, а не только в текущей точке.

Прогноз повторяет арифметику модели (`model/resource_env.py::transition`):
солнце и целевая температура известны из сценария на всю смену, поэтому
это честный расчёт, а не эвристика. Все параметры читаются из
``scenario["model"]`` — ничего не хардкодится.

Стоимость: прогноз строится один раз на шаг для пары «аппарат × режим
нагрузки» (ожидание / relay / downlink), а не на каждое задание. На
P04_demand это 6 аппаратов × 3 режима вместо 8120 заданий.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from backend.core.index import ScenarioIndex

SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class EnergyForecast:
    """Сколько шагов работы аппарат может оплатить, не уйдя под резерв."""

    step: int
    horizon: int
    # (satellite_id, kind) -> число шагов работы, проходящих по теплу;
    # kind: "downlink" | "relay". Проверку по заряду делает covers().
    affordable: dict[tuple[str, str], int]
    # (satellite_id) -> прогноз SOC в конце горизонта при ожидании, %
    idle_soc_pct: dict[str, float]
    # (satellite_id) -> нарастающий минимум энергии траектории ожидания:
    # floor[i] = min(E_idle[0..i]). Нужен, чтобы оценивать запас не по
    # всему горизонту, а по той его части, где работа реально пройдёт.
    floor_wh: dict[str, list[float]] = field(default_factory=dict)
    # (satellite_id, kind) -> расход Вт*ч за шаг работы
    per_step_wh: dict[tuple[str, str], float] = field(default_factory=dict)
    reserve_wh: dict[str, float] = field(default_factory=dict)
    # (satellite_id, kind) -> шаги, оплачиваемые по худшей точке горизонта.
    # Пессимистичная оценка: её использует теневая цена энергии, которой
    # нужен ответ «связывает ли ресурс», а не точное число.
    energy_steps: dict[tuple[str, str], int] = field(default_factory=dict)

    def energy_steps_for(self, sid: str, kind: str) -> int:
        return self.energy_steps.get((sid, kind), 0)

    def steps_for(self, sid: str, kind: str) -> int:
        return self.affordable.get((sid, kind), 0)

    def covers(self, sid: str, kind: str, need_steps: int) -> bool:
        """Хватит ли заряда, чтобы отработать ``need_steps`` шагов.

        Работа ляжет на первые ``need_steps`` шагов горизонта — по ним и
        берётся минимум траектории. Оценивать запас по минимуму всего
        горизонта чрезмерно строго: на P03_energy такая версия отказывала
        в прибыльной работе и стоила $964 выручки (см. findings.md).
        """
        if need_steps <= 0:
            return True
        floors = self.floor_wh.get(sid)
        if not floors or need_steps > len(floors):
            return self.steps_for(sid, kind) >= need_steps
        if need_steps > self.steps_for(sid, kind):
            return False        # уже не проходит по теплу
        per_step = self.per_step_wh.get((sid, kind), 0.0)
        reserve = self.reserve_wh.get(sid, 0.0)
        return floors[need_steps - 1] - need_steps * per_step >= reserve - 1e-9

    def min_soc_pct(self, sid: str, floor_pct: float,
                    max_buffer_pct: float = 8.0) -> float:
        """Порог начала работы для бортового агента (З5).

        Берётся не константой: порог поднимается над резервом ровно на
        предсказанную нехватку. Если прогноз ожидания уводит аппарат под
        резерв, борту нужен запас; если смена проходит на солнце, порог
        равен резерву и осторожничать незачем.

        Важно, что это НЕ «работать только выше прогнозного SOC»: такой
        порог запрещал бы работу на всей просадке, а именно там она и
        нужна. На P03_energy ошибочная версия стоила $7 237 при загрузке
        раз в шаг (см. findings.md).
        """
        predicted = self.idle_soc_pct.get(sid)
        if predicted is None:
            return floor_pct
        shortfall = max(0.0, floor_pct - predicted)
        return round(floor_pct + min(max_buffer_pct, shortfall), 2)


def _payload_w(index: ScenarioIndex, sid: str, kind: str | None) -> float:
    if kind is None:
        return 0.0
    return float(index.sats[sid][kind + "_w"])


def simulate(index: ScenarioIndex, sid: str, energy_wh: float, temp_c: float,
             step: int, horizon: int, kind: str | None) -> tuple[list[float], list[float]]:
    """Траектория (энергия, температура) при постоянной нагрузке ``kind``.

    Повторяет `Environment.transition`: солнце, нагреватель, КПД заряда и
    разряда, экспоненциальное приближение к равновесной температуре.
    """
    sat = index.sats[sid]
    model = index.model
    env = index.env[sid]
    dt = float(index.step_s)
    capacity = float(sat["capacity_wh"])
    payload_w = _payload_w(index, sid, kind)

    energies: list[float] = []
    temps: list[float] = []
    stop = min(index.steps, step + horizon)
    for k in range(step, stop):
        heater = float(sat["heater_w"]) if temp_c < model["heater_below_c"] else 0.0
        load_w = float(sat["base_w"]) + heater + payload_w
        solar = float(env["solar_w"][k])
        delta = (solar - load_w) * dt / SECONDS_PER_HOUR
        if delta >= 0:
            delta = (delta * model["charge_efficiency"]
                     if model["charge_min_c"] <= temp_c <= model["charge_max_c"]
                     else 0.0)
        else:
            delta /= model["discharge_efficiency"]
        energy_wh = min(capacity, max(0.0, energy_wh + delta))
        equilibrium = float(env["thermal_target_c"][k]) + \
            float(model["thermal_gain_c_per_w"]) * load_w
        temp_c = equilibrium + (temp_c - equilibrium) * math.exp(-dt / model["thermal_tau_s"])
        energies.append(energy_wh)
        temps.append(temp_c)
    return energies, temps


def build(index: ScenarioIndex, session, step: int, horizon: int) -> EnergyForecast:
    """Прогноз на горизонт для всех аппаратов и обоих типов нагрузки."""
    model = index.model
    affordable: dict[tuple[str, str], int] = {}
    idle_soc: dict[str, float] = {}
    floor_prefix: dict[str, list[float]] = {}
    per_step_wh: dict[tuple[str, str], float] = {}
    reserves: dict[str, float] = {}
    energy_steps: dict[tuple[str, str], int] = {}

    for sid in index.sat_ids:
        state = session.env.state[sid]
        capacity = float(index.sats[sid]["capacity_wh"])
        reserve_wh = capacity * float(model["reserve_soc_pct"]) / 100.0

        energies, _ = simulate(index, sid, state["energy_wh"], state["temp_c"],
                               step, horizon, None)
        idle_soc[sid] = (100.0 * energies[-1] / capacity if energies
                         else index.soc_pct(sid, state["energy_wh"]))

        # Нарастающий минимум траектории ожидания: работа может прийтись
        # ровно на просадку, и тогда аппарат упрётся в резерв в середине
        # задания, а не в конце горизонта.
        floors: list[float] = []
        running = state["energy_wh"]
        for value in energies:
            running = min(running, value)
            floors.append(running)
        floor_prefix[sid] = floors
        reserves[sid] = reserve_wh
        headroom = max(0.0, (floors[-1] if floors else state["energy_wh"]) - reserve_wh)

        for kind in ("downlink", "relay"):
            per_step = (float(index.sats[sid][kind + "_w"]) * float(index.step_s)
                        / SECONDS_PER_HOUR / model["discharge_efficiency"])
            per_step_wh[(sid, kind)] = per_step
            by_energy = int(headroom // per_step) if per_step > 0 else horizon

            # Тепло восстанавливается в простое, поэтому шаги считаются, а не
            # обрываются на первом горячем: это верхняя граница по теплу.
            _, temps = simulate(index, sid, state["energy_wh"], state["temp_c"],
                                step, horizon, kind)
            by_thermal = sum(
                1 for t in temps
                if model["payload_min_c"] <= t <= model["payload_max_c"])

            affordable[(sid, kind)] = max(0, by_thermal)
            energy_steps[(sid, kind)] = max(0, by_energy)

    return EnergyForecast(step=step, horizon=horizon, affordable=affordable,
                          idle_soc_pct=idle_soc, floor_wh=floor_prefix,
                          per_step_wh=per_step_wh, reserve_wh=reserves,
                          energy_steps=energy_steps)
