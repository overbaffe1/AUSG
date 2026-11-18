"""Интерфейс планировщика.

Контракт: ``decide`` возвращает действия для всех аппаратов, уже
согласованные по глобальным ограничениям. Планировщик обязан сам
разрешать конфликты — «не рассчитывать на отбор конфликтов библиотекой»
(описание данных, с. 8). Планировщик НЕ изменяет состояние среды.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Objective = Literal["priority_service", "commercial"]
Mode = Literal["centralized", "federated"]

IDLE: dict[str, Any] = {"action": "idle"}
CALIBRATE: dict[str, Any] = {"action": "calibrate"}


def job_action(job_id: str) -> dict[str, Any]:
    return {"action": "job", "job_id": job_id}


@dataclass(frozen=True)
class PlannerConfig:
    objective: Objective = "priority_service"
    mode: Mode = "centralized"
    rule: str = "edf"                     # для baseline: edf | value | priority
    lookahead_steps: int = 12
    calibration_margin_steps: int = 3
    soc_safety_margin_pct: float = 2.0
    urgency_gamma: float = 2.0
    critical_weight: float = 1000.0
    protect_gateway: bool = True
    # Допуск (З1). tightness = остаток работы / число оставшихся окон.
    # Выше порога у задания нет права на ошибку: один отказ по теплу или
    # заряду — и потраченные шаги пропадают, частичная работа не оплачивается.
    admission_tightness_limit: float = 1.0
    # Вытеснение (З2). Порог безразличия задан ДОЛЕЙ стоимости вытесняемого
    # задания, а не суммой в долларах: жюри может подать сценарий с другим
    # масштабом цен, и абсолютный порог там ничего не будет значить.
    preemption_hysteresis_frac: float = 0.1
    # Минимальная выдержка задания на аппарате. Задания в выданных данных
    # занимают 1-3 шага, поэтому выдержка в 2 шага запретила бы вытеснение
    # почти везде и стоила бы $1 448 на P04_demand (см. findings.md).
    preemption_min_hold_steps: int = 1
    # Сглаживание теневых цен (З3): доля новой оценки в экспоненциальном
    # среднем. 1.0 — без сглаживания. Значение 0.2 выбрано замером
    # (experiments/stability.py): нестабильность решений на P04_demand
    # падает с 26.7% до 21.8%, выручка при этом растёт.
    price_smoothing: float = 0.2
    # Назначение (З4): greedy — жадно по чистой ценности, optimal —
    # венгерский алгоритм на двудольном графе с ограничением по топ-N
    # кандидатов на аппарат (полная матрица на 8120 заданиях неподъёмна).
    assignment: str = "greedy"
    assignment_top_per_sat: int = 6
    # Федеративный режим: горизонт полётного задания и минимальный интервал
    # между загрузками. Интервал — главный параметр цены автономии: чем реже
    # аппарат получает обновление, тем дороже обходится самостоятельность.
    bundle_horizon_steps: int = 48
    replan_gap_steps: int = 4
    seed: int = 0
    weights: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "objective": self.objective, "mode": self.mode, "rule": self.rule,
            "lookahead_steps": self.lookahead_steps,
            "calibration_margin_steps": self.calibration_margin_steps,
            "soc_safety_margin_pct": self.soc_safety_margin_pct,
            "urgency_gamma": self.urgency_gamma,
            "critical_weight": self.critical_weight,
            "protect_gateway": self.protect_gateway,
            "admission_tightness_limit": self.admission_tightness_limit,
            "preemption_hysteresis_frac": self.preemption_hysteresis_frac,
            "preemption_min_hold_steps": self.preemption_min_hold_steps,
            "price_smoothing": self.price_smoothing,
            "assignment": self.assignment,
            "assignment_top_per_sat": self.assignment_top_per_sat,
            "bundle_horizon_steps": self.bundle_horizon_steps,
            "replan_gap_steps": self.replan_gap_steps,
            "seed": self.seed, "weights": dict(self.weights),
        }


@dataclass
class Decision:
    """Решение по одному аппарату на одном шаге."""

    action: dict[str, Any]
    score: float = 0.0
    reason_code: str = "idle"
    explain: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"action": self.action, "score": round(self.score, 6),
                "reason_code": self.reason_code, "explain": self.explain}


class Planner(Protocol):
    name: str
    version: str

    def decide(self, session, index, cfg: PlannerConfig) -> dict[str, Decision]:
        ...


# Коды причин простоя. Не смешиваются между собой — это прямое требование
# постановки: причины простоя, отказа в операции и срыва задания различны.
REASONS = {
    "accepted": "действие принято",
    "idle": "ожидание",
    "no_eligible_job": "нет доступного задания для этого аппарата",
    "energy_recovery": "восстановление заряда: запас у резерва",
    "thermal_hold": "температура вне рабочего диапазона",
    "ground_capacity_reserved": "слот наземного сегмента отдан более ценному заданию",
    "gateway_hold": "аппарат удерживает связь с Землёй",
    "gateway_protected": "калибровка запрещена: единственный канал связи",
    "calibration_required": "просрочена калибровка",
    "calibration_preventive": "профилактическая калибровка в простое",
    "satellite_unavailable": "аппарат недоступен",
    "infeasible_by_contacts": "окон связи меньше, чем осталось работы",
    "infeasible_by_deadline": "срок наступит раньше, чем работа может быть завершена",
    # Формулировка правлена по внешнему аудиту (З8.3): теневые цены —
    # внутренняя оценка ценности ресурса, а не расходы в долларах. Отказ
    # означает «нашлось применение лучше», а не «операция убыточна».
    "negative_value": "ресурс уступает более ценному применению по оценке планировщика",
    "no_completion_margin": "запас до срока меньше допустимого: доводить некогда",
    "energy_forecast_short": "прогноз заряда не покрывает оставшуюся работу",
    "preempted_by": "вытеснено более ценным заданием",
    "rejected_by_model": "действие отклонено проверкой модели",
}
