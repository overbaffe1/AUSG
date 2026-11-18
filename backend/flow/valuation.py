"""Оценка задания: деньги и срочность.

Срочность считается не по календарю, а через дефицит возможностей:
сколько шагов остаётся, где задание вообще можно выполнять, против того,
сколько работы осталось. Это же даёт проверяемое основание для утверждения
о невыполнимости (критерий О3).
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.core.index import ScenarioIndex

# Приоритет и стоимость — разные характеристики задания (описание данных, с. 5).
# Приоритет НЕ является множителем стоимости.
PRIORITY_CRITICAL = 3


@dataclass(frozen=True)
class Assessment:
    job_id: str
    value_usd: float
    remaining_steps: int
    opportunities: int
    tightness: float
    urgency: float
    yield_per_step: float
    provably_infeasible: bool
    reason: str | None


def opportunities(index: ScenarioIndex, job: dict, step: int,
                  unavailable: dict[str, tuple[int, int]] | None = None) -> int:
    """Число шагов в [step, deadline), где хотя бы один исполнитель на связи.

    Считается объединение по допустимым исполнителям: задание можно вести
    разными аппаратами на разных шагах (для relay смена исполнителя разрешена).
    """
    kind = job["kind"]
    stop = min(int(job["deadline_step"]), index.steps)
    start = max(int(step), int(job["release_step"]))
    if start >= stop:
        return 0

    eligible = job["eligible_satellites"]
    count = 0
    for k in range(start, stop):
        for sid in eligible:
            if not index.env[sid][kind + "_available"][k]:
                continue
            if unavailable:
                window = unavailable.get(sid)
                if window and window[0] <= k < window[1]:
                    continue
            count += 1
            break
    return count


def assess(index: ScenarioIndex, job: dict, step: int, *,
           gamma: float = 2.0,
           unavailable: dict[str, tuple[int, int]] | None = None) -> Assessment:
    """Полная оценка задания на текущем шаге."""
    remaining = int(job.get("remaining_steps", job["work_steps"]))
    windows = opportunities(index, job, step, unavailable)

    tightness = remaining / max(1, windows)
    infeasible = windows < remaining
    reason = None
    if infeasible:
        reason = ("infeasible_by_contacts" if windows > 0
                  else "infeasible_by_deadline")

    # Чем плотнее, тем выше срочность. При tightness >= 1 задание должно
    # идти немедленно на каждом оставшемся окне — жёсткое закрепление.
    urgency = min(tightness, 1.0) ** gamma if tightness < 1.0 else 1.0

    return Assessment(
        job_id=job["id"],
        value_usd=float(job["value_usd"]),
        remaining_steps=remaining,
        opportunities=windows,
        tightness=round(tightness, 4),
        urgency=round(urgency, 6),
        yield_per_step=round(float(job["value_usd"]) / max(1, remaining), 4),
        provably_infeasible=infeasible,
        reason=reason,
    )


def priority_tier(priority: int, objective: str) -> int:
    """Разряд задания в выбранном режиме. Сравнение лексикографическое.

    priority_service — задания приоритета 3 доминируют: сначала сравнивается
        разряд, и только при равенстве — деньги.
    commercial — приоритет не влияет на выбор, результат по приоритету 3
        показывается отдельно, чтобы оператор видел последствия.

    Почему разряд, а не множитель (правка внешнего аудита, З8.2). Раньше
    было ``gross = value_usd * 1000`` для приоритета 3. Задание приоритета 3
    со стоимостью 0 давало ноль при любом множителе и не получало никакого
    преимущества. В выданных данных таких заданий нет (проверено: 0 во всех
    четырёх сценариях), но схема допускает ``value_usd >= 0``, и жюри может
    подать свой сценарий. Лексикографическое сравнение точнее отвечает
    формулировке организаторов «завершить в срок как можно больше заданий
    приоритета 3»: оно не зависит от масштаба цен вообще.
    """
    if objective == "priority_service" and priority == PRIORITY_CRITICAL:
        return 1
    return 0


def priority_multiplier(priority: int, objective: str,
                        critical_weight: float = 1000.0) -> float:
    """Устаревшее: множитель приоритета.

    Оставлено ради совместимости вызовов; выбор ведётся по
    ``priority_tier``. Всегда возвращает 1.0, потому что масштабирование
    выручки приоритетом — это ровно тот дефект, который исправлен в З8.2.
    """
    return 1.0
