"""Индексы сценария.

Зачем: ``Environment.observation()`` делает deepcopy всех заданий. На
P04_demand это 8120 заданий x 288 шагов — вызов в цикле убивает
производительность. Планировщик читает ``env.state`` / ``env.jobs``
напрямую и ведёт собственные инкрементальные индексы.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScenarioIndex:
    """Статическая часть сценария плюс инкрементально обновляемое окно заданий."""

    steps: int
    step_s: int
    model: dict
    sats: dict[str, dict]
    sat_ids: list[str]
    env: dict

    jobs_by_release: dict[int, list[str]] = field(default_factory=dict)
    jobs_by_sat: dict[str, set[str]] = field(default_factory=dict)
    active: set[str] = field(default_factory=set)

    _gateway_cache: dict[int, list[str]] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- построение

    @classmethod
    def build(cls, scenario: dict) -> "ScenarioIndex":
        sats = {v["id"]: v for v in scenario["satellites"]}
        idx = cls(
            steps=scenario["time"]["steps"],
            step_s=scenario["time"]["step_s"],
            model=scenario["model"],
            sats=sats,
            sat_ids=sorted(sats),
            env=scenario["environment"],
        )
        idx.jobs_by_sat = {sid: set() for sid in sats}
        for job in scenario["jobs"]:
            idx.register(job)
        return idx

    def register(self, job: dict) -> None:
        """Доиндексировать задание. Вызывается и для событий add_jobs."""
        self.jobs_by_release.setdefault(job["release_step"], []).append(job["id"])
        for sid in job["eligible_satellites"]:
            self.jobs_by_sat[sid].add(job["id"])

    # ---------------------------------------------------------------- окно заданий

    def refresh(self, step: int, jobs: dict[str, dict]) -> None:
        """Обновить множество активных заданий на шаге ``step``.

        Активное = выпущено, не завершено, срок ещё не наступил.
        """
        for job_id in self.jobs_by_release.get(step, ()):
            self.active.add(job_id)
        stale = [
            job_id for job_id in self.active
            if (job := jobs.get(job_id)) is None
            or job["completed_step"] is not None
            or job["remaining_steps"] <= 0
            or job["deadline_step"] <= step
        ]
        self.active.difference_update(stale)

    def candidates_for(self, sid: str, jobs: dict[str, dict]) -> list[dict]:
        """Активные задания, которые этот аппарат в принципе может взять."""
        return [jobs[j] for j in self.active & self.jobs_by_sat[sid] if j in jobs]

    # ---------------------------------------------------------------- доступность

    def available(self, sid: str, kind: str, step: int) -> bool:
        """Доступен ли канал нужного типа. kind: 'downlink' | 'relay'."""
        return bool(self.env[sid][kind + "_available"][step])

    def contacts(self, step: int) -> list[str]:
        """Аппараты, видящие Землю на этом шаге. Кандидаты в шлюзы."""
        cached = self._gateway_cache.get(step)
        if cached is None:
            cached = [s for s in self.sat_ids
                      if self.env[s]["downlink_available"][step]]
            self._gateway_cache[step] = cached
        return cached

    def remaining_windows(self, sid: str, kind: str, start: int, stop: int) -> int:
        """Сколько шагов канала остаётся у аппарата в полуинтервале [start, stop)."""
        row = self.env[sid][kind + "_available"]
        return sum(1 for k in range(max(0, start), min(stop, self.steps)) if row[k])

    # ---------------------------------------------------------------- энергия

    def usable_wh(self, sid: str, energy_wh: float) -> float:
        """Запас энергии сверх резерва — то, чем действительно можно распорядиться."""
        sat = self.sats[sid]
        reserve = sat["capacity_wh"] * self.model["reserve_soc_pct"] / 100
        return max(0.0, energy_wh - reserve)

    def soc_pct(self, sid: str, energy_wh: float) -> float:
        return 100.0 * energy_wh / self.sats[sid]["capacity_wh"]
