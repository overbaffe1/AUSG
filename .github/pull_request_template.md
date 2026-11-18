## Описание изменений

<!-- Кратко: что сделано и почему -->

## Слой и область

- [ ] Ядро планирования (`backend/flow/`, `backend/planners/`, `backend/autonomy/`)
- [ ] API и схемы (`backend/app/`)
- [ ] Консоль оператора (`frontend/`)
- [ ] Документация / эксперименты (`README.md`, `docs/`, `experiments/`)

## Проверка

- [ ] `python -m pytest` — все тесты проходят (без правок эталонной библиотеки `model/`)
- [ ] `cd frontend && npm run build` — сборка без ошибок TypeScript/Vite
- [ ] При изменении логики допусков/цен проверены замеры `experiments/run_benchmarks.py`
