"""Хранение сценариев: встроенные из data/, загруженные и производные."""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any

from model.resource_env import validate

from backend import config
from backend.errors import NotFound, ValidationFailed, explain_model_error


@dataclass
class ScenarioRecord:
    id: str
    scenario: dict
    source: str                     # "builtin" | "upload" | "derived"
    derived_from: str | None = None
    derivation: dict[str, Any] | None = None

    def summary(self) -> dict:
        s = self.scenario
        jobs = s["jobs"]
        by_kind: dict[str, int] = {}
        by_priority: dict[int, int] = {}
        for j in jobs:
            by_kind[j["kind"]] = by_kind.get(j["kind"], 0) + 1
            by_priority[j["priority"]] = by_priority.get(j["priority"], 0) + 1
        return {
            "id": self.id,
            "title": s["meta"]["title"],
            "source": self.source,
            "derived_from": self.derived_from,
            "derivation": self.derivation,
            "steps": s["time"]["steps"],
            "step_s": s["time"]["step_s"],
            "duration_hours": round(s["time"]["steps"] * s["time"]["step_s"] / 3600, 2),
            "satellites": len(s["satellites"]),
            "jobs_total": len(jobs),
            "jobs_by_kind": by_kind,
            "jobs_by_priority": {str(k): v for k, v in sorted(by_priority.items())},
            "value_total_usd": round(sum(j["value_usd"] for j in jobs), 2),
            "known_failures": len(s["failures"]),
        }


class ScenarioStore:
    def __init__(self) -> None:
        self._items: dict[str, ScenarioRecord] = {}
        self._load_builtin()

    def _load_builtin(self) -> None:
        if not config.DATA_DIR.is_dir():
            return
        for path in sorted(config.DATA_DIR.glob("*.json")):
            try:
                scenario = json.loads(path.read_text(encoding="utf-8-sig"))
                validate(scenario)
            except Exception:  # битый файл в data/ не должен ронять сервис
                continue
            sid = scenario["meta"]["id"]
            self._items[sid] = ScenarioRecord(id=sid, scenario=scenario,
                                              source="builtin")

    # ------------------------------------------------------------------ доступ

    def list(self) -> list[dict]:
        return [r.summary() for r in self._items.values()]

    def get(self, scenario_id: str) -> ScenarioRecord:
        try:
            return self._items[scenario_id]
        except KeyError:
            raise NotFound(f"Сценарий {scenario_id!r} не найден",
                           {"known": sorted(self._items)}) from None

    # ------------------------------------------------------------------ запись

    def add_upload(self, raw: dict) -> ScenarioRecord:
        try:
            validate(raw)
        except Exception as exc:
            raise ValidationFailed(
                "Сценарий не прошёл проверку: " + explain_model_error(exc)) from None
        base = str(raw["meta"]["id"])
        sid = base if base not in self._items else f"{base}-{uuid.uuid4().hex[:6]}"
        scenario = copy.deepcopy(raw)
        scenario["meta"]["id"] = sid
        record = ScenarioRecord(id=sid, scenario=scenario, source="upload")
        self._items[sid] = record
        return record

    def derive(self, scenario_id: str, *, initial_soc: dict[str, float] | None = None,
               solar_factor: float | None = None,
               job_priority: dict[str, int] | None = None,
               title: str | None = None) -> ScenarioRecord:
        """Изменить допустимые начальные условия и сохранить как новый сценарий.

        Допускается ровно то, что разрешает постановка: начальный заряд аппарата,
        коэффициент солнечной мощности, приоритет существующего задания.

        Важно: множитель солнца меняет ТОЛЬКО solar_w. Прямого правила
        "больше солнца — выше температура" в модели нет, тепловые ряды
        не трогаются (описание данных, с. 5).
        """
        parent = self.get(scenario_id)
        scenario = copy.deepcopy(parent.scenario)
        derivation: dict[str, Any] = {}

        if initial_soc:
            known = {v["id"] for v in scenario["satellites"]}
            unknown = set(initial_soc) - known
            if unknown:
                raise ValidationFailed("Неизвестные аппараты",
                                       {"satellites": sorted(unknown)})
            for sat in scenario["satellites"]:
                if sat["id"] in initial_soc:
                    value = float(initial_soc[sat["id"]])
                    if not 0.0 <= value <= 100.0:
                        raise ValidationFailed(
                            f"Начальный заряд {sat['id']} вне диапазона 0..100%")
                    sat["initial_soc_pct"] = value
            derivation["initial_soc_pct"] = dict(initial_soc)

        if solar_factor is not None:
            factor = float(solar_factor)
            if factor < 0:
                raise ValidationFailed("Коэффициент солнечной мощности не может быть отрицательным")
            for row in scenario["environment"].values():
                row["solar_w"] = [x * factor for x in row["solar_w"]]
            derivation["solar_factor"] = factor

        if job_priority:
            by_id = {j["id"]: j for j in scenario["jobs"]}
            unknown = set(job_priority) - set(by_id)
            if unknown:
                raise ValidationFailed("Неизвестные задания", {"jobs": sorted(unknown)})
            for job_id, priority in job_priority.items():
                if int(priority) not in (1, 2, 3):
                    raise ValidationFailed("Приоритет задания должен быть 1, 2 или 3")
                by_id[job_id]["priority"] = int(priority)
            derivation["job_priority"] = {k: int(v) for k, v in job_priority.items()}

        if not derivation:
            raise ValidationFailed("Не указано ни одного изменения")

        sid = f"{parent.id}-var-{uuid.uuid4().hex[:6]}"
        scenario["meta"]["id"] = sid
        scenario["meta"]["title"] = title or f"{parent.scenario['meta']['title']} (вариант)"
        try:
            validate(scenario)
        except Exception as exc:
            raise ValidationFailed(
                "Изменённый сценарий не прошёл проверку: " + explain_model_error(exc)) from None

        record = ScenarioRecord(id=sid, scenario=scenario, source="derived",
                                derived_from=parent.id, derivation=derivation)
        self._items[sid] = record
        return record


STORE = ScenarioStore()
