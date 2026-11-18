"""Бортовой агент: выбор действия ЛОКАЛЬНО, без обращения к Земле.

Аппарат знает только собственное состояние (заряд, температура, возраст
калибровки, доступность канала) и свой бандл. Вне контакта он живёт по
последнему загруженному полётному заданию — это и есть автономность.

Демо-кейс: P01_intro, где группировка не видит Землю 85% смены,
максимальный разрыв — 37 шагов подряд (более трёх часов).

ЭТАП 2 (Dev A).
"""
from __future__ import annotations

from backend.autonomy.bundle import Bundle
from backend.core.index import ScenarioIndex
from backend.planners.base import CALIBRATE, IDLE, Decision, job_action


def decide_onboard(session, index: ScenarioIndex, sid: str, bundle: Bundle | None,
                   calibration_margin: int,
                   skip_speculative: bool = True) -> Decision:
    """Одно решение одного аппарата на одном шаге."""
    env = session.env
    step = env.k
    state = env.state[sid]
    valid = int(index.model["calibration_valid_steps"])
    explain = {"step": step, "source": "onboard",
               "bundle_issued_at": bundle.issued_at_step if bundle else None,
               "bundle_expired": bundle.expired(step) if bundle else True}

    if not env.available(sid):
        return Decision(IDLE, 0.0, "satellite_unavailable", explain)

    # 1. Роль шлюза важнее калибровки: потерять единственный канал нельзя.
    is_gateway = bool(bundle and step in bundle.gateway_steps)
    explain["is_gateway"] = is_gateway

    age = state["calibration_age_steps"]

    # 2. Калибровка по сроку — но не шлюзу.
    if age >= valid - calibration_margin and not is_gateway:
        ok, reason, _ = env.can_execute(sid, CALIBRATE)
        if ok:
            return Decision(CALIBRATE, 0.0, "calibration_required", explain)
        explain["calibration_blocked_by"] = reason

    # 3. Первый элемент очереди, проходящий локальные проверки.
    if bundle and not bundle.expired(step):
        soc = index.soc_pct(sid, state["energy_wh"])
        # Порядок очереди — тот, что задала Земля. Пробовали ставить
        # начатое вперёд («доделай, потом берись за новое»): на бортах это
        # проигрывает, потому что очередь и так отсортирована по сроку, а
        # начатое дешёвое задание задерживает срочное. Замер: P04_demand
        # при загрузке раз в 4 шага $147 594 -> $135 617 (findings.md).
        for item in bundle.queue:
            job = env.jobs.get(item.job_id)
            if job is None or job["completed_step"] is not None:
                continue
            # Условие отказа борт проверяет сам, без связи с Землёй:
            # задание, которое уже не успеть, только тратит ресурс (З5).
            abandon = item.abandon_reason(index, sid, job["kind"], step,
                                          int(job["remaining_steps"]))
            if abandon is not None:
                explain.setdefault("abandoned", []).append(
                    {"job_id": item.job_id, "reason": abandon})
                continue
            # allowed_at учитывает и окно, и заранее выделенные слоты
            # наземного сегмента, и фору основного исполнителя: договориться
            # на лету борта не могут.
            if not item.allowed_at(step):
                continue
            # Признак «не окупится», предвычисленный на Земле: элемент
            # лежит в бандле сверх ёмкости борта. Начинать такое —
            # тратить энергию на работу, которая не будет оплачена.
            # Начатое добиваем: вложенные шаги иначе пропадут.
            started = int(job["remaining_steps"]) < int(job["work_steps"])
            if skip_speculative and item.speculative and not started:
                explain.setdefault("skipped", []).append(
                    {"job_id": item.job_id, "reason": "speculative"})
                continue
            if soc < item.min_soc_pct:
                explain.setdefault("skipped", []).append(
                    {"job_id": item.job_id, "reason": "min_soc_pct"})
                continue
            ok, reason, _ = env.can_execute(sid, job_action(item.job_id))
            if not ok:
                explain.setdefault("skipped", []).append(
                    {"job_id": item.job_id, "reason": reason})
                continue
            explain["chosen_job"] = item.job_id
            explain["backup_rank"] = item.backup_rank
            return Decision(job_action(item.job_id), 0.0, "accepted", explain)

    if is_gateway:
        return Decision(IDLE, 0.0, "gateway_hold", explain)

    # 4. Fallback: профилактика, если есть запас и смысл.
    if age > valid // 2:
        ok, _, _ = env.can_execute(sid, CALIBRATE)
        if ok:
            return Decision(CALIBRATE, 0.0, "calibration_preventive", explain)

    return Decision(IDLE, 0.0, "no_eligible_job", explain)
