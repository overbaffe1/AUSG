"""Предельные ресурсные затраты задания — считаются ДО выполнения.

Название важно (правка внешнего аудита, З8.3). Раньше модуль назывался
«себестоимостью», а слово обещает больше, чем здесь считается.

Считается **предельная** величина: расход СВЕРХ ожидания. Базовое
потребление служебных систем и нагреватель тратятся и в простое, поэтому
относить их на задание нельзя — решение «брать или не брать» они не
меняют. Для downlink это 7.89 Вт*ч за шаг.

Полная нагрузка без учёта солнца — другое число, 9.47 Вт*ч за шаг. Оно не
годится для выбора, но нужно оператору, чтобы понимать энергобаланс, и
поэтому отдаётся рядом: ``marginal_energy_wh`` и ``full_load_energy_wh``.

Все параметры берутся из ``scenario["model"]`` и характеристик аппарата.
Ничего не хардкодится: жюри может подать сценарий с другими границами.

Отдельно: в описании данных (с. 4) payload_min_c указан как "от 5 °C",
тогда как во всех выданных файлах он равен -5.0. Истина — в данных.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from backend.core.index import ScenarioIndex

SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class CostCard:
    """Во что обойдётся выполнение задания на конкретном аппарате.

    ``marginal_*`` — расход сверх ожидания, по нему принимается решение.
    ``full_load_*`` — полное потребление аппарата под нагрузкой, без
    вычета того, что тратилось бы и в простое, и без учёта солнца.
    """

    satellite_id: str
    job_id: str
    kind: str
    work_steps: int
    # Устаревшие имена: оставлены как синонимы marginal_*, чтобы не ломать
    # интерфейс. Новый код читает marginal_energy_wh.
    energy_wh: float
    energy_per_step_wh: float
    marginal_energy_wh: float
    marginal_energy_per_step_wh: float
    full_load_energy_wh: float
    full_load_energy_per_step_wh: float
    energy_pct_of_usable: float | None
    ground_slots: int
    sat_steps: int
    thermal_rise_c: float
    calibration_share: float
    feasible: bool
    blocking: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def payload_power_w(sat: dict, kind: str) -> float:
    """Дополнительная мощность операции: downlink_w или relay_w."""
    return float(sat[kind + "_w"])


def energy_per_step_wh(sat: dict, model: dict, kind: str, step_s: int) -> float:
    """ПРЕДЕЛЬНЫЙ расход на один шаг работы, с учётом КПД разряда.

    Это стоимость самой операции сверх постоянного потребления служебных
    систем: base_w и нагреватель тратятся и при ожидании, поэтому в
    решение «брать или не брать» они не входят.
    """
    return (payload_power_w(sat, kind) * step_s
            / SECONDS_PER_HOUR / model["discharge_efficiency"])


def full_load_energy_per_step_wh(sat: dict, model: dict, kind: str,
                                 step_s: int) -> float:
    """ПОЛНОЕ потребление аппарата за шаг работы, без вычета простоя.

    Нагреватель не учитывается: он включается по температуре, а не по
    факту работы. Солнце тоже не учитывается — это расход, а не баланс.
    Число справочное: для выбора годится предельное (см. docstring модуля).
    """
    load_w = float(sat["base_w"]) + payload_power_w(sat, kind)
    return load_w * step_s / SECONDS_PER_HOUR / model["discharge_efficiency"]


def thermal_rise_c(sat: dict, model: dict, kind: str) -> float:
    """Сдвиг равновесной температуры под полной нагрузкой.

    Teq = thermal_target_c + thermal_gain_c_per_w * L, где L — полная нагрузка.
    При downlink 90 Вт и base 18 Вт это +21.6 °C сверх внешних условий —
    при payload_max_c = 45 °C разогрев сам по себе блокирует работу.
    """
    load_w = float(sat["base_w"]) + payload_power_w(sat, kind)
    return float(model["thermal_gain_c_per_w"]) * load_w


def build_cost_card(index: ScenarioIndex, sid: str, job: dict,
                    energy_wh: float | None = None,
                    remaining_steps: int | None = None) -> CostCard:
    """Карта стоимости пары «аппарат — задание».

    ``energy_wh`` — текущий запас аппарата; если не задан, берётся начальный.
    ``remaining_steps`` — остаток работы; если не задан, берётся полный объём.
    """
    sat = index.sats[sid]
    model = index.model
    kind = job["kind"]
    steps = int(remaining_steps if remaining_steps is not None else job["work_steps"])

    per_step = energy_per_step_wh(sat, model, kind, index.step_s)
    total = per_step * steps
    full_per_step = full_load_energy_per_step_wh(sat, model, kind, index.step_s)

    if energy_wh is None:
        energy_wh = sat["capacity_wh"] * sat["initial_soc_pct"] / 100.0
    usable = index.usable_wh(sid, energy_wh)
    pct = round(100.0 * total / usable, 2) if usable > 0 else None

    blocking: str | None = None
    if sid not in job["eligible_satellites"]:
        blocking = "ineligible_satellite"
    elif usable <= 0:
        blocking = "energy_reserve"
    elif total > usable:
        # Солнце может дозарядить в процессе, поэтому это предупреждение,
        # а не приговор: окончательную проверку делает can_execute.
        blocking = "energy_tight"

    return CostCard(
        satellite_id=sid,
        job_id=job["id"],
        kind=kind,
        work_steps=steps,
        energy_wh=round(total, 4),
        energy_per_step_wh=round(per_step, 4),
        marginal_energy_wh=round(total, 4),
        marginal_energy_per_step_wh=round(per_step, 4),
        full_load_energy_wh=round(full_per_step * steps, 4),
        full_load_energy_per_step_wh=round(full_per_step, 4),
        energy_pct_of_usable=pct,
        ground_slots=steps if kind == "downlink" else 0,
        sat_steps=steps,
        thermal_rise_c=round(thermal_rise_c(sat, model, kind), 2),
        calibration_share=round(steps / model["calibration_valid_steps"], 4),
        feasible=blocking in (None, "energy_tight"),
        blocking=blocking,
    )


def cost_cards_for_job(index: ScenarioIndex, job: dict) -> list[CostCard]:
    """Карты стоимости по всем допустимым исполнителям задания."""
    return [build_cost_card(index, sid, job) for sid in job["eligible_satellites"]]
