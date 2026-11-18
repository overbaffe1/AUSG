"""Показатели сверх Session.summary().

Правила учёта из описания данных (с. 10-11), нарушение = потеря баллов:
  - доля выполнения приоритетных заданий при нулевом знаменателе
    ОТСУТСТВУЕТ, а не равна 0% или 100% — в UI прочерк, не ноль;
  - загрузка аппарата — доля шагов ЗАДАНИЙ; ожидание и калибровка
    учитываются отдельно;
  - удаление невыполненных заданий из знаменателя или переоценка их
    стоимости после исполнения не допускаются.
"""
from __future__ import annotations

from backend.flow.explain import idle_breakdown


def utilisation(run) -> dict[str, dict]:
    """Загрузка каждого аппарата за исполненный период."""
    counters: dict[str, dict[str, int]] = {}
    for row in run.session.env.trace:
        sid = row["satellite_id"]
        bucket = counters.setdefault(sid, {"job": 0, "calibrate": 0, "idle": 0})
        bucket[row["executed"]] = bucket.get(row["executed"], 0) + 1

    result: dict[str, dict] = {}
    for sid, bucket in counters.items():
        total = sum(bucket.values())
        result[sid] = {
            "job_steps": bucket["job"],
            "calibrate_steps": bucket["calibrate"],
            "idle_steps": bucket["idle"],
            "executed_steps": total,
            # При нуле исполненных шагов доля отсутствует.
            "job_share": round(bucket["job"] / total, 4) if total else None,
        }
    return result


def blocked_reasons(run) -> dict[str, int]:
    """Распределение причин отказа модели по журналу."""
    counts: dict[str, int] = {}
    for row in run.session.env.trace:
        reason = row["reason"]
        if reason in ("accepted", "idle"):
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def ground_link_usage(run) -> dict:
    """Использование лимита наземного сегмента по шагам."""
    limit = int(run.index.model["downlink_parallel_limit"])
    per_step: dict[int, int] = {}
    for row in run.session.env.trace:
        if row["executed"] == "job":
            job = run.session.env.jobs.get(row["requested"].get("job_id"))
            if job and job["kind"] == "downlink":
                per_step[row["step"]] = per_step.get(row["step"], 0) + 1
    used = sum(per_step.values())
    executed_steps = run.step
    return {
        "limit_per_step": limit,
        "slots_available": limit * executed_steps,
        "slots_used": used,
        "utilisation": round(used / (limit * executed_steps), 4)
        if executed_steps else None,
        "saturated_steps": sum(1 for v in per_step.values() if v >= limit),
    }


def priority_service(run) -> dict:
    """Результат по приоритету 3 — показывается отдельно в обоих режимах."""
    summary = run.session.summary()
    due = summary["critical_jobs_due"]
    done = summary["critical_jobs_completed_on_time"]
    return {
        "critical_jobs_due": due,
        "critical_jobs_completed_on_time": done,
        # Знаменатель ноль -> показателя нет.
        "share": round(done / due, 4) if due else None,
    }


def report(run) -> dict:
    summary = run.session.summary()
    return {
        "run_id": run.id,
        "step": run.step,
        "summary": summary,
        "priority_service": priority_service(run),
        "utilisation": utilisation(run),
        "blocked_reasons": blocked_reasons(run),
        "idle_reasons": idle_breakdown(run),
        "ground_link": ground_link_usage(run),
        "lost_work": {
            "work_steps_in_missed_jobs": summary["work_steps_in_missed_jobs"],
            "jobs_due_missed": summary["jobs_due_missed"],
        },
        "energy": {
            "below_reserve_satellite_steps": summary["below_reserve_satellite_steps"],
            "critical_soc_satellite_steps": summary["critical_soc_satellite_steps"],
            "brownout_satellite_steps": summary["brownout_satellite_steps"],
            "minimum_soc_pct": summary["minimum_soc_pct"],
        },
    }


def compare(run_a, run_b) -> dict:
    """Сравнение двух ветвей: дельты и вердикт."""
    a, b = run_a.session.summary(), run_b.session.summary()
    fields = ("revenue_usd", "jobs_completed", "jobs_due_missed",
              "critical_jobs_completed_on_time", "critical_jobs_due",
              "work_steps_in_missed_jobs", "minimum_soc_pct")
    deltas = {f: round(b.get(f, 0) - a.get(f, 0), 6) for f in fields}

    objective = run_a.cfg.objective
    verdict = _verdict(objective, a, b, run_a.label or run_a.id,
                       run_b.label or run_b.id)
    return {
        "a": {"run_id": run_a.id, "label": run_a.label,
              "objective": run_a.cfg.objective, "mode": run_a.cfg.mode,
              "planner": run_a.planner_name, "summary": a},
        "b": {"run_id": run_b.id, "label": run_b.label,
              "objective": run_b.cfg.objective, "mode": run_b.cfg.mode,
              "planner": run_b.planner_name, "summary": b},
        "deltas": deltas,
        "comparable_state": run_a.parent_run_id == run_b.parent_run_id
        or run_a.id == run_b.parent_run_id or run_b.id == run_a.parent_run_id,
        "verdict": verdict,
    }


def _verdict(objective: str, a: dict, b: dict, name_a: str, name_b: str) -> dict:
    """Какой вариант предпочтительнее — или честное «сопоставимо»."""
    if objective == "priority_service":
        primary = "critical_jobs_completed_on_time"
        label = "заданий приоритета 3 в срок"
    else:
        primary = "revenue_usd"
        label = "выручка"

    diff = b[primary] - a[primary]
    tie_break = b["revenue_usd"] - a["revenue_usd"]

    # Порог сопоставимости: 1% от большего значения, но не меньше единицы.
    scale = max(abs(a[primary]), abs(b[primary]), 1.0)
    if abs(diff) <= 0.01 * scale:
        if objective == "priority_service" and abs(tie_break) > 0.01 * max(
                abs(a["revenue_usd"]), 1.0):
            better = name_b if tie_break > 0 else name_a
            return {"outcome": "tie_broken_by_revenue",
                    "preferred": better,
                    "text": f"По основному показателю ({label}) результаты сопоставимы; "
                            f"решает выручка — лучше «{better}»."}
        return {"outcome": "comparable", "preferred": None,
                "text": f"Результаты сопоставимы по выбранной цели ({label}). "
                        f"Разные настройки не обязаны давать разные расписания."}

    better = name_b if diff > 0 else name_a
    return {"outcome": "preferred", "preferred": better,
            "text": f"Для выбранной цели предпочтительнее «{better}»: "
                    f"{label} отличается на {abs(diff):.4g}."}
