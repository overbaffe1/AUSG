"""HTTP-слой. Тонкий: вся логика живёт в flow/, planners/, analytics/."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Query, Request

from model.operations import replay_episode

from backend import config
from backend.analytics import metrics
from backend.app.schemas import (AdvanceRequest, BranchRequest, CreateRunRequest,
                                 DeriveRequest, ManualEventRequest,
                                 ObjectiveRequest, PlannerConfigIn)
from backend.core.index import ScenarioIndex
from backend.core.registry import PLANNERS, REGISTRY
from backend.core.scenario_store import STORE
from backend.errors import NotFound, ServiceError, ValidationFailed, explain_model_error
from backend.flow import preflight
from backend.flow.cost import cost_cards_for_job
from backend.flow.explain import decision_report

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- сценарии

@router.get("/scenarios")
def list_scenarios() -> dict:
    return {"scenarios": STORE.list()}


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str) -> dict:
    return STORE.get(scenario_id).summary()


@router.post("/scenarios/upload")
async def upload_scenario(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise ValidationFailed("Файл слишком большой",
                               {"limit_bytes": config.MAX_UPLOAD_BYTES})
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed(f"Не удалось разобрать JSON: {exc}") from None
    return STORE.add_upload(payload).summary()


@router.post("/scenarios/{scenario_id}/derive")
def derive_scenario(scenario_id: str, body: DeriveRequest) -> dict:
    record = STORE.derive(
        scenario_id,
        initial_soc=body.initial_soc_pct,
        solar_factor=body.solar_factor,
        job_priority=body.job_priority,
        title=body.title,
    )
    return record.summary()


@router.get("/scenarios/{scenario_id}/preflight")
def scenario_preflight(scenario_id: str) -> dict:
    return preflight.analyse(STORE.get(scenario_id).scenario)


@router.get("/scenarios/{scenario_id}/jobs/{job_id}/cost")
def job_cost(scenario_id: str, job_id: str) -> dict:
    scenario = STORE.get(scenario_id).scenario
    job = next((j for j in scenario["jobs"] if j["id"] == job_id), None)
    if job is None:
        raise NotFound(f"Задание {job_id!r} не найдено в сценарии {scenario_id!r}")
    index = ScenarioIndex.build(scenario)
    return {"job": job,
            "cost_cards": [c.to_dict() for c in cost_cards_for_job(index, job)]}


# ---------------------------------------------------------------- прогоны

def _config_from(body: PlannerConfigIn | None, objective: str, mode: str):
    from backend.planners.base import PlannerConfig
    data = (body or PlannerConfigIn()).model_dump()
    return PlannerConfig(objective=objective, mode=mode, **data)


@router.get("/runs")
def list_runs() -> dict:
    return {"runs": REGISTRY.list()}


@router.post("/runs")
def create_run(body: CreateRunRequest) -> dict:
    cfg = _config_from(body.config, body.objective, body.mode)
    run = REGISTRY.create(body.scenario_id, cfg, body.planner, body.label)
    return run.state()


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return REGISTRY.get(run_id).state()


@router.delete("/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    REGISTRY.get(run_id)
    REGISTRY.drop(run_id)
    return {"deleted": run_id}


@router.post("/runs/{run_id}/advance")
def advance_run(run_id: str, body: AdvanceRequest) -> dict:
    run = REGISTRY.get(run_id)
    with run.lock:
        if body.to_end:
            steps = run.index.steps - run.step
        elif body.to_step is not None:
            steps = body.to_step - run.step
            if steps < 0:
                raise ValidationFailed(
                    "Нельзя вернуться назад: выполненные шаги не пересчитываются",
                    {"current_step": run.step, "requested": body.to_step})
        else:
            steps = body.steps or 1
        rows = run.advance(steps)
    return {"executed_rows": len(rows), "state": run.state()}


@router.post("/runs/{run_id}/objective")
def change_objective(run_id: str, body: ObjectiveRequest) -> dict:
    run = REGISTRY.get(run_id)
    with run.lock:
        run.set_objective(body.objective)
    return {"objective": run.cfg.objective, "history": run.objective_changes}


# ---------------------------------------------------------------- события

@router.post("/runs/{run_id}/events")
def post_event(run_id: str, payload: dict[str, Any] = Body(...)) -> dict:
    """Принимает один объект события ИЛИ файл cosmo-B-events-1.0 целиком.

    Жюри на проверке передаёт отдельные сообщения, поэтому одиночный
    объект — основной путь. Обёртка поддерживается для открытого примера.
    """
    run = REGISTRY.get(run_id)
    if payload.get("schema_version") == config.EVENT_SCHEMA:
        events = payload.get("events") or []
        if not isinstance(events, list):
            raise ValidationFailed("Поле events должно быть списком")
    else:
        events = [payload]

    accepted, pending = [], []
    with run.lock:
        for event in events:
            # Несколько сообщений на одном шаге обрабатываются в порядке
            # получения; события будущих шагов не применяются заранее.
            if event.get("at_step") != run.step:
                pending.append({"id": event.get("id"), "at_step": event.get("at_step")})
                continue
            accepted.append(run.apply_event(event))
    return {"accepted": accepted, "not_for_this_step": pending,
            "state": run.state()}


@router.post("/runs/{run_id}/events/manual")
def post_manual_event(run_id: str, body: ManualEventRequest) -> dict:
    """Ручной ввод: выбор аппаратов и конца интервала. at_step ставит сервер."""
    run = REGISTRY.get(run_id)
    with run.lock:
        event = {
            "id": body.id or f"UI-{body.type}-{run.step}-{len(run.session.events) + 1}",
            "at_step": run.step,
            "type": body.type,
            "satellite_ids": body.satellite_ids,
            "end_step": body.end_step,
        }
        result = run.apply_event(event)
    return {"accepted": result, "event": event, "state": run.state()}


# ---------------------------------------------------------------- наблюдение

@router.get("/runs/{run_id}/flow")
def job_flow(run_id: str, limit: int = Query(500, ge=1, le=5000),
             status: str | None = None) -> dict:
    """Поток заданий: принято / в работе / ждёт / отклонено с причиной."""
    run = REGISTRY.get(run_id)
    step = run.step
    buckets: dict[str, list[dict]] = {"in_progress": [], "waiting": [],
                                      "completed": [], "missed": []}
    for job in run.session.env.jobs.values():
        done = job["completed_step"] is not None
        started = job["remaining_steps"] < job["work_steps"]
        overdue = job["deadline_step"] <= step and not done
        bucket = ("completed" if done else "missed" if overdue
                  else "in_progress" if started else "waiting")
        buckets[bucket].append({
            "job_id": job["id"], "kind": job["kind"],
            "priority": job["priority"], "value_usd": job["value_usd"],
            "release_step": job["release_step"], "deadline_step": job["deadline_step"],
            "work_steps": job["work_steps"], "remaining_steps": job["remaining_steps"],
            "progress": round(1 - job["remaining_steps"] / job["work_steps"], 4),
            "completed_step": job["completed_step"],
            "eligible_satellites": job["eligible_satellites"],
        })
    counts = {k: len(v) for k, v in buckets.items()}
    if status:
        if status not in buckets:
            raise ValidationFailed("Неизвестный статус",
                                   {"known": sorted(buckets)})
        buckets = {status: buckets[status]}
    return {"step": step, "counts": counts,
            "jobs": {k: v[:limit] for k, v in buckets.items()}}


@router.get("/runs/{run_id}/trace")
def get_trace(run_id: str, from_step: int = 0, to_step: int | None = None,
              satellite_id: str | None = None, reason: str | None = None,
              limit: int = Query(1000, ge=1)) -> dict:
    run = REGISTRY.get(run_id)
    limit = min(limit, config.TRACE_PAGE_LIMIT)
    rows = []
    for row in run.session.env.trace:
        if row["step"] < from_step:
            continue
        if to_step is not None and row["step"] > to_step:
            continue
        if satellite_id and row["satellite_id"] != satellite_id:
            continue
        if reason and row["reason"] != reason:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return {"rows": rows, "returned": len(rows),
            "total_rows": len(run.session.env.trace)}


@router.get("/runs/{run_id}/explain")
def explain_decision(run_id: str, step: int, satellite_id: str,
                     full: bool = False) -> dict:
    """Разбор решения. ``full=true`` восстанавливает отклонённых и карту
    стоимости у старого шага — это повтор прогона, до пяти секунд на
    P04_demand. Без него ответ мгновенный, а поле ``detail_available``
    говорит, есть ли уже глубокая часть."""
    return decision_report(REGISTRY.get(run_id), step, satellite_id, full)


@router.get("/runs/{run_id}/analytics")
def analytics(run_id: str) -> dict:
    return metrics.report(REGISTRY.get(run_id))


@router.get("/runs/{run_id}/bundles")
def bundles(run_id: str) -> dict:
    """Полётные задания: что загружено на борт, когда и до какого шага.

    Видно, что аппарат вне связи работает по последнему загруженному
    бандлу — это и есть автономность.
    """
    run = REGISTRY.get(run_id)
    snapshot = getattr(run.planner, "snapshot", None)
    if run.cfg.mode != "federated" or snapshot is None:
        return {"mode": run.cfg.mode, "bundles": [],
                "note": "Полётные задания формируются только в федеративном режиме"}
    return {"mode": run.cfg.mode, "step": run.step, **snapshot()}


# ---------------------------------------------------------------- сравнение

@router.post("/runs/{run_id}/branch")
def branch_run(run_id: str, body: BranchRequest) -> dict:
    parent = REGISTRY.get(run_id)
    cfg = _config_from(body.config or PlannerConfigIn(),
                       body.objective or parent.cfg.objective,
                       body.mode or parent.cfg.mode)
    child = REGISTRY.branch(run_id, cfg, body.planner or parent.planner_name,
                            body.label)
    return child.state()


@router.get("/compare")
def compare(a: str, b: str) -> dict:
    return metrics.compare(REGISTRY.get(a), REGISTRY.get(b))


# ---------------------------------------------------------------- выгрузка

@router.get("/runs/{run_id}/export")
def export_run(run_id: str, include_trace: bool = True) -> dict:
    """Машиночитаемая выгрузка cosmo-B-ops-result-1.0.

    trace в файле необязателен, если сведения восстанавливаются по
    обязательным полям (описание данных, с. 11). На P04_demand это
    экономит десятки мегабайт.
    """
    run = REGISTRY.get(run_id)
    with run.lock:
        run.session.run_metadata = run.run_metadata()
        result = run.session.result()
    if not include_trace:
        result.pop("trace", None)
    return result


@router.post("/runs/import")
def import_run(payload: dict[str, Any] = Body(...)) -> dict:
    """Загрузить выгрузку и подтвердить, что расчёт воспроизводится."""
    if payload.get("schema_version") != config.RESULT_SCHEMA:
        raise ValidationFailed("Неподдерживаемая схема выгрузки",
                               {"expected": config.RESULT_SCHEMA})
    try:
        session = replay_episode(payload["initial_scenario"], payload["events"],
                                 payload["commands"], payload["steps_executed"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationFailed(
            "Не удалось воспроизвести расчёт: " + explain_model_error(exc)) from None

    expected = payload.get("summary", {})
    actual = session.summary()
    mismatches = {k: {"saved": expected[k], "replayed": actual.get(k)}
                  for k in expected if expected[k] != actual.get(k)}
    return {
        "reproduced": not mismatches,
        "steps_executed": session.env.k,
        "summary": actual,
        "mismatches": mismatches,
        "message": ("Расчёт воспроизведён, показатели совпали" if not mismatches
                    else "Показатели расходятся — см. mismatches"),
    }


# ---------------------------------------------------------------- служебное

@router.get("/planners")
def list_planners() -> dict:
    return {"planners": [{"name": name, "version": cls.version}
                         for name, cls in PLANNERS.items()]}
