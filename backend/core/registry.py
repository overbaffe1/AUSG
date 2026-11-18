"""Реестр прогонов. Данные разных запусков и пользователей не смешиваются."""
from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from model.operations import Session

from backend import config
from backend.core.index import ScenarioIndex
from backend.core.scenario_store import STORE
from backend.errors import (EventRejected, NotFound, RunFinished, ServiceError,
                            ValidationFailed, explain_model_error)
from backend.planners.base import Decision, PlannerConfig
from backend.planners.baseline import BaselinePlanner
from backend.planners.economic import EconomicPlanner
from backend.planners.federated import FederatedPlanner

PLANNERS = {"baseline": BaselinePlanner, "economic": EconomicPlanner}


def make_planner(planner_name: str, cfg: PlannerConfig):
    """Алгоритм выбирает планировщик, режим — способ исполнения.

    centralized — ЦУП решает за каждый аппарат на каждом шаге.
    federated   — ЦУП формирует полётные задания в окнах связи,
                  дальше аппараты решают сами.
    """
    if cfg.mode == "federated":
        return FederatedPlanner(horizon_steps=cfg.bundle_horizon_steps,
                                replan_gap=cfg.replan_gap_steps)
    return PLANNERS[planner_name]()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Run:
    id: str
    scenario_id: str
    session: Session
    index: ScenarioIndex
    cfg: PlannerConfig
    planner_name: str
    created_at: str
    label: str = ""
    parent_run_id: str | None = None
    branch_step: int | None = None
    objective_changes: list[dict] = field(default_factory=list)
    decisions: dict[int, dict[str, dict]] = field(default_factory=dict)
    # Принятые сообщения с шагом получения: без них пересчёт старого шага
    # разойдётся с историей (З6).
    events: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Планировщик хранится, а не пересоздаётся: федеративный режим держит
    # полётные задания между шагами.
    planner: object | None = field(default=None, repr=False)
    # Кэш восстановленных разборов старых шагов (см. recompute_decision).
    _recompute_cache: dict[int, dict] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ план

    @property
    def step(self) -> int:
        return self.session.env.k

    @property
    def finished(self) -> bool:
        return self.step >= self.index.steps

    # ------------------------------------------------------------------ шаги

    def advance(self, steps: int) -> list[dict]:
        """Выполнить ``steps`` шагов. Решения принимает планировщик."""
        if self.finished:
            raise RunFinished("Смена уже завершена",
                              {"steps_executed": self.step})
        planner = self.planner or make_planner(self.planner_name, self.cfg)
        self.planner = planner
        rows: list[dict] = []
        for _ in range(steps):
            if self.finished:
                break
            current = self.step
            decisions = planner.decide(self.session, self.index, self.cfg)
            actions = {sid: d.action for sid, d in decisions.items()}
            self.decisions[current] = {sid: d.to_dict() for sid, d in decisions.items()}
            self._trim_old_decisions(current)
            rows.extend(self.session.advance(actions))
        return rows

    def _trim_old_decisions(self, current: int) -> None:
        """Оставить полный разбор только у последних шагов (З6).

        Полный журнал на P04_demand весит 26 МБ на прогон. Усечённая
        запись сохраняет всё, что нужно для показателей и списка причин;
        разбор кандидатов старого шага пересчитывается по запросу.
        """
        stale = current - config.DECISIONS_FULL_STEPS
        step_decisions = self.decisions.get(stale)
        if not step_decisions:
            return
        for decision in step_decisions.values():
            explain = decision.get("explain")
            if not isinstance(explain, dict) or explain.get("truncated"):
                continue
            # top_candidates остаётся всегда: это пять записей вида
            # {job_id, net_value}, и ровно их требует панель разбора
            # решения (критерий О3). Без них клик по середине смены на
            # P04_demand упирался в пересчёт с нуля — до 5 секунд на
            # шаге 220, тогда как панель обязана открываться за 300 мс.
            # Тяжёлое — rejected и detail с картой стоимости — по-прежнему
            # восстанавливается по запросу.
            for key in ("rejected", "detail"):
                explain.pop(key, None)
            explain["truncated"] = True

    def apply_event(self, event: dict) -> dict:
        """Принять одно сообщение на текущем шаге. Применяется атомарно."""
        try:
            self.session.apply_event(event)
        except (ValueError, KeyError, TypeError) as exc:
            raise EventRejected(
                "Сообщение отклонено: " + explain_model_error(exc),
                {"current_step": self.step, "event_id": event.get("id")}) from None
        # Новые задания нужно доиндексировать, чтобы планировщик их увидел.
        if event.get("type") == "add_jobs":
            for job in event["jobs"]:
                self.index.register(job)
        self.events.append(copy.deepcopy(event))
        return {"accepted": event.get("id"), "current_step": self.step}

    def recompute_decision(self, step: int) -> dict[str, dict] | None:
        """Восстановить полный разбор шага, который был усечён (З6).

        Прогон повторяется с нуля из исходного сценария: те же сообщения
        на тех же шагах, тот же планировщик, та же конфигурация. Смена
        цели воспроизводится по журналу objective_changes, поэтому
        результат совпадает с исходным шаг в шаг.
        """
        if step < 0 or step >= self.index.steps:
            return None
        # Восстановленный шаг больше не изменится: история только растёт,
        # а новые сообщения прошлых решений не переписывают. Поэтому
        # результат кэшируется — оператор обычно кликает по соседним
        # ячейкам, и каждый такой клик иначе стоил бы полного прогона.
        cached = self._recompute_cache.get(step)
        if cached is not None:
            return copy.deepcopy(cached)

        scenario = copy.deepcopy(self.session.initial_scenario)
        session = Session(scenario)
        index = ScenarioIndex.build(session.initial_scenario)
        cfg = self.cfg
        changes = {c["step"]: c for c in self.objective_changes}
        first = next(iter(self.objective_changes), None)
        if first is not None:
            cfg = PlannerConfig(**{**cfg.to_dict(), "objective": first["from"]})
        planner = make_planner(self.planner_name, cfg)
        by_step: dict[int, list[dict]] = {}
        for event in self.events:
            by_step.setdefault(int(event["at_step"]), []).append(event)

        for k in range(step + 1):
            if k in changes:
                cfg = PlannerConfig(**{**cfg.to_dict(), "objective": changes[k]["to"]})
            for event in by_step.get(k, ()):
                session.apply_event(event)
                if event.get("type") == "add_jobs":
                    for job in event["jobs"]:
                        index.register(job)
            decisions = planner.decide(session, index, cfg)
            if k == step:
                restored = {sid: d.to_dict() for sid, d in decisions.items()}
                if len(self._recompute_cache) >= config.RECOMPUTE_CACHE_STEPS:
                    self._recompute_cache.pop(next(iter(self._recompute_cache)))
                self._recompute_cache[step] = restored
                return copy.deepcopy(restored)
            session.advance({sid: d.action for sid, d in decisions.items()})
        return None

    def set_objective(self, objective: str) -> None:
        """Смена цели на границе шага: влияет только на дальнейшую работу."""
        if objective == self.cfg.objective:
            return
        self.objective_changes.append(
            {"step": self.step, "from": self.cfg.objective, "to": objective})
        self.cfg = PlannerConfig(**{**self.cfg.to_dict(), "objective": objective})

    # ------------------------------------------------------------------ вывод

    def run_metadata(self) -> dict:
        algorithm = (f"{self.planner_name}+federated" if self.cfg.mode == "federated"
                     else self.planner_name)
        version = (FederatedPlanner.version if self.cfg.mode == "federated"
                   else PLANNERS[self.planner_name].version)
        meta: dict[str, Any] = {
            "goal": self.cfg.objective,
            "mode": self.cfg.mode,
            "algorithm": algorithm,
            "version": version,
            "parameters": self.cfg.to_dict(),
            "app_version": config.APP_VERSION,
            "run_id": self.id,
            "scenario_id": self.scenario_id,
            "created_at": self.created_at,
        }
        if self.objective_changes:
            meta["objective_changes"] = self.objective_changes
        if self.parent_run_id:
            meta["branched_from"] = {"run_id": self.parent_run_id,
                                     "at_step": self.branch_step}
        record = STORE.get(self.scenario_id)
        if record.derivation:
            meta["scenario_derivation"] = {"parent": record.derived_from,
                                           "changes": record.derivation}
        return meta

    def state(self) -> dict:
        env = self.session.env
        sats = {}
        for sid, st in env.state.items():
            capacity = self.index.sats[sid]["capacity_wh"]
            sats[sid] = {
                "energy_wh": round(st["energy_wh"], 3),
                "soc_pct": round(100 * st["energy_wh"] / capacity, 2),
                "temp_c": round(st["temp_c"], 2),
                "calibration_age_steps": st["calibration_age_steps"],
                "calibration_valid_for": max(
                    0, int(self.index.model["calibration_valid_steps"])
                    - st["calibration_age_steps"]),
                "available": env.available(sid),
                "in_contact": self.index.available(sid, "downlink", min(
                    self.step, self.index.steps - 1)),
            }
        return {
            "run_id": self.id, "label": self.label,
            "scenario_id": self.scenario_id,
            "step": self.step, "steps_total": self.index.steps,
            "finished": self.finished,
            "objective": self.cfg.objective, "mode": self.cfg.mode,
            "planner": self.planner_name,
            "state_digest": self.session.state_digest(),
            "satellites": sats,
            "summary": self.session.summary(),
        }


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def create(self, scenario_id: str, cfg: PlannerConfig, planner: str,
               label: str = "") -> Run:
        if planner not in PLANNERS:
            raise ValidationFailed(f"Неизвестный планировщик {planner!r}",
                                   {"known": sorted(PLANNERS)})
        record = STORE.get(scenario_id)
        scenario = copy.deepcopy(record.scenario)
        run = Run(
            id=uuid.uuid4().hex[:12],
            scenario_id=scenario_id,
            session=Session(scenario),
            index=ScenarioIndex.build(scenario),
            cfg=cfg, planner_name=planner,
            created_at=_now(), label=label,
        )
        run.planner = make_planner(planner, cfg)
        run.session.run_metadata = run.run_metadata()
        with self._lock:
            if len(self._runs) >= config.MAX_ACTIVE_RUNS:
                oldest = min(self._runs.values(), key=lambda r: r.created_at)
                self._runs.pop(oldest.id, None)
            self._runs[run.id] = run
        return run

    def branch(self, run_id: str, cfg: PlannerConfig, planner: str,
               label: str = "") -> Run:
        """Независимое продолжение из того же фактического состояния.

        У ветвей совпадают шаг, заряд, температура, калибровка, прогресс
        заданий и предыстория — это гарантирует Session.fork().
        """
        parent = self.get(run_id)
        with parent.lock:
            forked = parent.session.fork()
            index = ScenarioIndex.build(parent.session.initial_scenario)
            # Восстановить индекс заданий, пришедших событиями.
            for job_id, job in forked.env.jobs.items():
                if job_id not in {j["id"] for j in parent.session.initial_scenario["jobs"]}:
                    index.register(job)
            child = Run(
                id=uuid.uuid4().hex[:12],
                scenario_id=parent.scenario_id,
                session=forked, index=index, cfg=cfg, planner_name=planner,
                created_at=_now(), label=label or f"ветвь от {parent.id}",
                parent_run_id=parent.id, branch_step=parent.step,
                objective_changes=list(parent.objective_changes),
                decisions=copy.deepcopy(parent.decisions),
                events=copy.deepcopy(parent.events),
            )
            # Ветвь получает собственную копию полётных заданий: продолжения
            # из одного состояния должны быть независимы.
            if parent.cfg.mode == cfg.mode and parent.planner is not None:
                child.planner = copy.deepcopy(parent.planner)
            else:
                child.planner = make_planner(child.planner_name, cfg)
        child.session.run_metadata = child.run_metadata()
        with self._lock:
            self._runs[child.id] = child
        return child

    def get(self, run_id: str) -> Run:
        try:
            return self._runs[run_id]
        except KeyError:
            raise NotFound(f"Расчёт {run_id!r} не найден") from None

    def list(self) -> list[dict]:
        return [{"run_id": r.id, "label": r.label, "scenario_id": r.scenario_id,
                 "step": r.step, "steps_total": r.index.steps,
                 "objective": r.cfg.objective, "mode": r.cfg.mode,
                 "planner": r.planner_name, "created_at": r.created_at,
                 "parent_run_id": r.parent_run_id}
                for r in sorted(self._runs.values(), key=lambda r: r.created_at,
                                reverse=True)]

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)


REGISTRY = RunRegistry()
