"""Назначение пар «аппарат — задание» с учётом общих ресурсов.

Вынесено отдельно, потому что это место, где решается конфликт за
дефицитный ресурс: слотов наземного сегмента всего два на всю
группировку, и отдавать их надо самым ценным заданиям.
"""
from __future__ import annotations

from typing import Callable, Iterable, TypeVar

T = TypeVar("T")


def _preference(verdict) -> tuple[int, float, str]:
    """Порядок предпочтения вердикта: разряд режима, затем деньги (З8.2).

    Разряд читается через getattr: сюда приходят и тестовые заглушки,
    у которых поля tier нет.
    """
    return (-getattr(verdict, "tier", 0), -verdict.net_value, verdict.job_id)


def reserve(candidates: Iterable[T], capacity: int,
            score: Callable[[T], float],
            key: Callable[[T], str]) -> tuple[list[T], list[T]]:
    """Отдать ограниченный ресурс лучшим кандидатам.

    Возвращает (принятые, вытесненные). Вытесненные получают причину
    ``ground_capacity_reserved`` — это честное объяснение простоя,
    а не молчаливый отказ среды.
    """
    ordered = sorted(candidates, key=lambda c: (-score(c), key(c)))
    return ordered[:max(0, capacity)], ordered[max(0, capacity):]


def assign_greedy(verdicts: Iterable, downlink_limit: int,
                  kind_of: Callable[[str], str]) -> dict[str, object]:
    """Жадное назначение: один аппарат — одно действие, одно задание — один исполнитель.

    ЭТАП 2 (Dev A): при запасе времени заменить на венгерский алгоритм
    на двудольном графе. Жадность теряет на случаях, где два аппарата
    конкурируют за одно дорогое задание, имея разные альтернативы.
    """
    budget = downlink_limit
    taken_jobs: set[str] = set()
    assigned: dict[str, object] = {}
    for verdict in sorted(verdicts, key=_preference):
        sid, job_id = verdict.satellite_id, verdict.job_id
        if sid in assigned or job_id in taken_jobs:
            continue
        if kind_of(job_id) == "downlink":
            if budget <= 0:
                continue
            budget -= 1
        assigned[sid] = verdict
        taken_jobs.add(job_id)
    return assigned


# --------------------------------------------------------------- венгерский

def hungarian(cost: list[list[float]]) -> list[int]:
    """Минимальная по сумме назначение строк на столбцы (JV-вариант, O(n^2*m)).

    Написано на месте, без новых зависимостей: матрица здесь крошечная
    (аппаратов единицы, кандидаты ограничены топ-N), и тянуть ради неё
    scipy в образ смысла нет — см. замер в findings.md.

    ``cost[i][j]`` — стоимость назначения строки i на столбец j.
    Возвращает для каждой строки номер столбца или -1, если строка не
    назначена (столбцов меньше, чем строк).
    """
    rows = len(cost)
    cols = len(cost[0]) if rows else 0
    if rows == 0 or cols == 0:
        return [-1] * rows
    if cols < rows:
        # Дополняем до квадрата нулевыми столбцами-«пустышками»: строка,
        # попавшая на пустышку, остаётся без назначения.
        padded = [row + [0.0] * (rows - cols) for row in cost]
        result = hungarian(padded)
        return [j if j < cols else -1 for j in result]

    inf = float("inf")
    u = [0.0] * (rows + 1)
    v = [0.0] * (cols + 1)
    match = [-1] * (cols + 1)      # столбец -> строка
    way = [0] * (cols + 1)

    for i in range(1, rows + 1):
        match[0] = i
        j0 = 0
        minv = [inf] * (cols + 1)
        used = [False] * (cols + 1)
        while True:
            used[j0] = True
            i0 = match[j0]
            delta = inf
            j1 = 0
            for j in range(1, cols + 1):
                if used[j]:
                    continue
                current = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if current < minv[j]:
                    minv[j] = current
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(cols + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if match[j0] == -1:
                break
        while j0:
            j1 = way[j0]
            match[j0] = match[j1]
            j0 = j1

    assignment = [-1] * rows
    for j in range(1, cols + 1):
        if match[j] != -1:
            assignment[match[j] - 1] = j - 1
    return assignment


def assign_optimal(verdicts: Iterable, downlink_limit: int,
                   kind_of: Callable[[str], str],
                   top_per_sat: int = 6) -> dict[str, object]:
    """Назначение на двудольном графе «аппараты x задания» с максимумом NV.

    Полная матрица на P04_demand неподъёмна (8120 заданий), поэтому
    рассматриваются только топ-N кандидатов на аппарат — на большее ни
    один аппарат всё равно не претендует: он берёт ровно одно задание.

    Лимит наземного сегмента вносится ремонтом: если решение назначило
    больше downlink, чем слотов, у аппарата с наименьшим вкладом downlink
    запрещается и задача решается заново. Итераций не больше числа
    аппаратов, потому что каждая снимает ровно один downlink.
    """
    by_sat: dict[str, list] = {}
    for verdict in sorted(verdicts, key=_preference):
        bucket = by_sat.setdefault(verdict.satellite_id, [])
        if len(bucket) < top_per_sat:
            bucket.append(verdict)

    sats = sorted(by_sat)
    if not sats:
        return {}
    jobs = sorted({v.job_id for bucket in by_sat.values() for v in bucket})
    job_index = {job_id: j for j, job_id in enumerate(jobs)}

    banned: set[tuple[str, str]] = set()
    for _ in range(len(sats) + 1):
        # Венгерский алгоритм минимизирует, мы максимизируем NV:
        # стоимость = -NV, недопустимая пара = 0 (лучше не назначать).
        # Разряд вносится слагаемым, заведомо большим любой разницы в
        # деньгах ВНУТРИ этой матрицы: так лексикографический порядок
        # сохраняется, а решать можно одной скалярной задачей. Величина
        # берётся из самих данных, а не константой.
        span = max((abs(v.net_value) for bucket in by_sat.values()
                    for v in bucket), default=0.0)
        tier_step = span * (len(sats) + 1) + 1.0

        cost = [[0.0] * len(jobs) for _ in sats]
        available: dict[tuple[int, int], object] = {}
        for i, sid in enumerate(sats):
            for verdict in by_sat[sid]:
                if (sid, verdict.job_id) in banned or verdict.net_value <= 0:
                    continue
                j = job_index[verdict.job_id]
                cost[i][j] = -(verdict.net_value
                               + getattr(verdict, "tier", 0) * tier_step)
                available[(i, j)] = verdict

        assignment = hungarian(cost)
        chosen: dict[str, object] = {}
        for i, j in enumerate(assignment):
            verdict = available.get((i, j)) if j >= 0 else None
            if verdict is not None:
                chosen[sats[i]] = verdict

        downlinks = [(sid, v) for sid, v in chosen.items()
                     if kind_of(v.job_id) == "downlink"]
        if len(downlinks) <= downlink_limit:
            return chosen
        # Снимаем самый дешёвый downlink и решаем заново.
        sid, verdict = min(downlinks,
                           key=lambda pair: (getattr(pair[1], "tier", 0),
                                             pair[1].net_value))
        banned.add((sid, verdict.job_id))

    return chosen
