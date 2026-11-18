# Замеры: что здесь лежит и как это получено

Каждый файл — результат одной команды. Выводы в `findings.md`, `README.md` и
презентации построены на этих данных; любое число можно проверить здесь, не
запуская расчёт заново.

| Файл | Строк | Что внутри | Чем получен |
|---|---|---|---|
| `benchmarks_full.csv` / `.json` | 80 | Полная матрица: 4 сценария × 2 цели × {без событий, `events_demo`} × {baseline `edf`/`value`/`priority` централизованно, economic централизованно, economic федеративно} | `python experiments/run_benchmarks.py --full` |
| `benchmarks.csv` / `.json` | — | Частичные прогоны: выборочные сценарии и развёртка интервала загрузки | `python experiments/run_benchmarks.py --scenarios … --autonomy-sweep …` |
| `stability.json` | 24 | Нестабильность решений против коэффициента сглаживания теневых цен | `python experiments/stability.py` |
| `gateway_policy.json` | 32 | Цена защиты шлюза: `protect_gateway` включён против выключенного при прочих равных | `python experiments/gateway_policy.py` |
| `uplink_cache.json` | 6 | Объём команд с кэшем полётных заданий и без него | `python experiments/uplink_cache.py` |
| `findings.md` | — | Разбор: что сделано, что измерено, где алгоритм проигрывает, что отвергнуто и почему | пишется вручную по данным выше |

## Колонки матрицы прогонов

`scenario`, `planner`, `mode`, `replan_gap`, `rule`, `objective`, `events`,
`events_applied`, `seconds`, `revenue_usd`, `jobs_completed`, `jobs_due_missed`,
`critical_due`, `critical_on_time`, `work_steps_in_missed_jobs`,
`blocked_commands`, `minimum_soc_pct`.

## Условия воспроизведения

- `seed = 0` во всех прогонах, настройки планировщика пишутся в `run_metadata`
  каждой выгрузки;
- сценарии — выданные `data/*.json` без изменений;
- события — `examples/events_demo.json`;
- измерено на обычном ноутбуке, время в колонке `seconds` — чистый счёт без
  подготовки объяснений.

Числа времени зависят от машины. Все остальные показатели детерминированы:
одна и та же команда на тех же данных даёт тот же результат.
