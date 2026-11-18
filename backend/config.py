"""Конфигурация сервиса. Никаких путей с машины разработчика (критерий Т5)."""
from __future__ import annotations

import os
from pathlib import Path

# Корень комплекта: на два уровня выше этого файла (backend/config.py -> корень).
ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.getenv("COSMO_DATA_DIR", ROOT / "data"))
EXAMPLES_DIR = Path(os.getenv("COSMO_EXAMPLES_DIR", ROOT / "examples"))
RUNS_DIR = Path(os.getenv("COSMO_RUNS_DIR", ROOT / "runs"))

# Версия приложения и планировщика попадают в run_metadata выгрузки.
APP_VERSION = os.getenv("COSMO_APP_VERSION", "0.1.0")

# Ограничения сервиса (не модели — те читаются из сценария).
MAX_UPLOAD_BYTES = int(os.getenv("COSMO_MAX_UPLOAD_BYTES", 64 * 1024 * 1024))
MAX_ACTIVE_RUNS = int(os.getenv("COSMO_MAX_ACTIVE_RUNS", 32))
TRACE_PAGE_LIMIT = int(os.getenv("COSMO_TRACE_PAGE_LIMIT", 5000))

# Сколько последних шагов хранят ПОЛНЫЙ разбор решения. У более ранних
# остаётся компактная запись без top_candidates и rejected: на P04_demand
# полный журнал весит 26 МБ на прогон, а при MAX_ACTIVE_RUNS = 32 это
# более 800 МБ. Полный разбор старого шага пересчитывается по запросу
# /explain (Run.recompute_decision).
DECISIONS_FULL_STEPS = int(os.getenv("COSMO_DECISIONS_FULL_STEPS", 24))

# Сколько восстановленных шагов держать в кэше /explain. Оператор кликает
# по соседним ячейкам расписания, и без кэша каждый клик стоил бы полного
# повторного прогона смены.
RECOMPUTE_CACHE_STEPS = int(os.getenv("COSMO_RECOMPUTE_CACHE_STEPS", 12))

# Схемы из описания данных.
SCENARIO_SCHEMA = "cosmo-B-ops-1.0"
EVENT_SCHEMA = "cosmo-B-events-1.0"
RESULT_SCHEMA = "cosmo-B-ops-result-1.0"
