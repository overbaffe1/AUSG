"""Схемы запросов и ответов. Общая точка Dev A и Dev B — менять по согласованию."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Objective = Literal["priority_service", "commercial"]
Mode = Literal["centralized", "federated"]


class DeriveRequest(BaseModel):
    """Допустимые изменения начальных условий — ровно то, что разрешает постановка."""

    initial_soc_pct: dict[str, float] | None = Field(
        default=None, description="Начальный заряд выбранных аппаратов, 0..100")
    solar_factor: float | None = Field(
        default=None, ge=0, description="Множитель солнечной мощности; меняет только solar_w")
    job_priority: dict[str, int] | None = Field(
        default=None, description="Новый приоритет существующих заданий, 1..3")
    title: str | None = None


class PlannerConfigIn(BaseModel):
    rule: str = "edf"
    lookahead_steps: int = Field(default=12, ge=1, le=288)
    calibration_margin_steps: int = Field(default=3, ge=0, le=48)
    soc_safety_margin_pct: float = Field(default=2.0, ge=0, le=50)
    urgency_gamma: float = Field(default=2.0, gt=0, le=10)
    critical_weight: float = Field(default=1000.0, ge=1)
    protect_gateway: bool = True
    bundle_horizon_steps: int = Field(default=48, ge=1, le=288)
    replan_gap_steps: int = Field(
        default=4, ge=1, le=288,
        description="Минимальный интервал между загрузками полётного задания. "
                    "Главный параметр цены автономии.")
    seed: int = 0
    weights: dict[str, float] = Field(default_factory=dict)


class CreateRunRequest(BaseModel):
    scenario_id: str
    objective: Objective = "priority_service"
    mode: Mode = "centralized"
    planner: str = "economic"
    label: str = ""
    config: PlannerConfigIn = Field(default_factory=PlannerConfigIn)


class BranchRequest(BaseModel):
    objective: Objective | None = None
    mode: Mode | None = None
    planner: str | None = None
    label: str = ""
    config: PlannerConfigIn | None = None


class AdvanceRequest(BaseModel):
    steps: int | None = Field(default=None, ge=1)
    to_step: int | None = Field(default=None, ge=0)
    to_end: bool = False


class ObjectiveRequest(BaseModel):
    objective: Objective


class ManualEventRequest(BaseModel):
    """Ручной ввод из интерфейса: пользователь не редактирует внутреннее состояние."""

    type: Literal["satellite_outage", "close_downlink"]
    satellite_ids: list[str] = Field(min_length=1)
    end_step: int = Field(ge=1)
    id: str | None = None


class EventEnvelope(BaseModel):
    """Приходит либо один объект события, либо файл cosmo-B-events-1.0.

    Жюри на проверке передаёт отдельные сообщения без обёртки.
    """

    model_config = {"extra": "allow"}

    schema_version: str | None = None
    base_scenario: str | None = None
    events: list[dict[str, Any]] | None = None
