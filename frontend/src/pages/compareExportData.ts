import { api, Mode, Objective, RunState } from '../api/client'

export const CHECKPOINT_PREFIX = 'Контрольная точка · '
export const objectiveLabel = (value: Objective) => value === 'commercial' ? 'Коммерческая отдача' : 'Приоритетное обслуживание'
export const modeLabel = (value: Mode) => value === 'federated' ? 'Бортовая автономия' : 'Централизованное управление'
export interface ExportRecord {
  schema_version: string
  initial_scenario_hash: string
  initial_scenario: Record<string, unknown>
  steps_executed: number
  events: Record<string, unknown>[]
  commands: Record<string, unknown>[]
  summary: Record<string, unknown>
  run_metadata: {
    parameters: Record<string, unknown>
    version?: string
    algorithm?: string
    goal?: string
    mode?: string
    run_id?: string
    branched_from?: { run_id: string; at_step: number }
  }
}
export function recordOf(value: unknown): ExportRecord {
  const record = value as ExportRecord
  if (!record || record.schema_version !== 'cosmo-B-ops-result-1.0' || !Array.isArray(record.commands) || !Array.isArray(record.events) || !Number.isInteger(record.steps_executed) || !record.initial_scenario || !record.summary || !record.run_metadata?.parameters) throw new Error('Сервер вернул неполную выгрузку расчёта.')
  return record
}
export interface RunEntry { run_id: string; label: string; scenario_id: string; step: number; steps_total: number; parent_run_id: string | null }
export function checkpointEntries(value: unknown[]): RunEntry[] {
  return value.filter((item): item is RunEntry => !!item && typeof item === 'object' && typeof (item as RunEntry).run_id === 'string' && typeof (item as RunEntry).label === 'string' && (item as RunEntry).label.startsWith(CHECKPOINT_PREFIX))
}

const ordered = (value: unknown): unknown => Array.isArray(value) ? value.map(ordered) : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => [key, ordered(child)])) : value
export const equalData = (a: unknown, b: unknown) => JSON.stringify(ordered(a)) === JSON.stringify(ordered(b))

export async function saveCheckpoint(source: RunState, label: string) {
  const fresh = await api.run(source.run_id)
  if (fresh.state_digest !== source.state_digest || fresh.step !== source.step) throw new Error('Исходный расчёт изменился. Обновите его перед сохранением контрольной точки.')
  const record = recordOf(await api.exportRun(source.run_id, false))
  const child = await api.branch(source.run_id, { label: `${CHECKPOINT_PREFIX}${label.trim() || `${source.scenario_id}, шаг ${source.step}`}`, objective: source.objective, mode: source.mode, planner: source.planner, config: record.run_metadata.parameters })
  if (child.state_digest !== source.state_digest || child.step !== source.step) throw new Error('Состояние изменилось во время сохранения. Созданная ветвь не используется для сравнения; повторите сохранение.')
  const saved = recordOf(await api.exportRun(child.run_id, false))
  if (!equalData(record.events, saved.events) || !equalData(record.commands, saved.commands) || record.initial_scenario_hash !== saved.initial_scenario_hash) throw new Error('Не удалось подтвердить сохранение выполненной истории контрольной точки.')
  return child
}

export interface ComparisonResponse {
  a: { run_id: string; label: string; objective: Objective; mode: Mode; planner: string; summary: Record<string, unknown> }
  b: { run_id: string; label: string; objective: Objective; mode: Mode; planner: string; summary: Record<string, unknown> }
  deltas: Record<string, number>
  comparable_state: boolean
  verdict: { outcome: string; preferred: string | null; text: string }
}
export function verifyBranches(source: RunState, initial: ExportRecord, a: RunState, b: RunState, ea: ExportRecord, eb: ExportRecord, target: number) {
  if (a.step !== target || b.step !== target || ea.steps_executed !== target || eb.steps_executed !== target || a.scenario_id !== source.scenario_id || b.scenario_id !== source.scenario_id) throw new Error('Ветви рассчитаны до разных границ или относятся к разным сценариям. Сравнение не показано.')
  for (const record of [ea, eb]) {
    const parent = record.run_metadata.branched_from
    if (parent?.run_id !== source.run_id || parent.at_step !== source.step || record.initial_scenario_hash !== initial.initial_scenario_hash || !equalData(record.events, initial.events) || !equalData(record.commands.filter(command => Number(command.step) < source.step), initial.commands)) throw new Error('Не подтверждены общий источник, одинаковые сообщения и история ветвей. Создайте сравнение заново.')
  }
}

export function downloadText(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement('a')
  link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 10000)
}
const esc = (value: unknown) => String(value ?? '—').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!)
const readable = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('ru-RU', { maximumFractionDigits: 6 }) : typeof value === 'object' && value !== null ? JSON.stringify(value) : value ?? '—'
const summaryNames: Record<string, string> = { revenue_usd: 'Выручка, USD', jobs_total: 'Всего известных заданий', jobs_completed: 'Завершено заданий', jobs_due: 'Задания с наступившим сроком', jobs_due_missed: 'Просрочено заданий', critical_jobs_due: 'P3 с наступившим сроком', critical_jobs_completed_on_time: 'P3 в срок', minimum_soc_pct: 'Минимальный SOC, %', work_steps_in_missed_jobs: 'Рабочие шаги в просроченных заданиях', below_reserve_satellite_steps: 'Аппарат-шаги ниже резерва', critical_soc_satellite_steps: 'Аппарат-шаги ниже критического заряда', brownout_satellite_steps: 'Обесточивания, аппарат-шаги', blocked_command_count: 'Отклонённые команды' }
export function reportHtml(run: RunState, record: ExportRecord) {
  const tableRows = Object.entries(record.summary).map(([key, value]) => `<tr><th>${esc(summaryNames[key] ?? key)}</th><td>${esc(readable(value))}</td></tr>`).join('')
  const satelliteRows = Object.entries(run.satellites).map(([id, state]) => `<tr><td>${esc(id)}</td><td>${esc(readable(state.soc_pct))}</td><td>${esc(readable(state.temp_c))}</td><td>${esc(state.calibration_age_steps)}</td><td>${state.available ? 'Да' : 'Нет'}</td></tr>`).join('')
  const events = record.events.map(event => `<tr><td>${esc(event.id)}</td><td>${esc(event.at_step)}</td><td>${esc(event.type)}</td><td>${esc(Array.isArray(event.satellite_ids) ? event.satellite_ids.join(', ') : Array.isArray(event.jobs) ? `${event.jobs.length} новых заданий` : '')}</td></tr>`).join('')
  return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Отчёт — ${esc(run.label || run.run_id)}</title><style>body{font:15px/1.55 system-ui,sans-serif;color:#132c38;max-width:1000px;margin:40px auto;padding:0 28px}h1{font-size:30px}h2{margin-top:30px}table{width:100%;border-collapse:collapse;margin:14px 0}th,td{border-bottom:1px solid #d4dce0;padding:9px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{font-weight:600}p{overflow-wrap:anywhere}.meta{color:#52626b}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}thead{display:table-header-group}tr{break-inside:avoid}@media print{body{margin:0;max-width:none;padding:0;font-size:11pt}h2{break-after:avoid}table{font-size:10pt}@page{margin:18mm}}</style></head><body><p class="meta">Команда «Космос» · Отчёт расчёта</p><h1>${esc(run.label || run.scenario_id)}</h1><p>Расчёт: ${esc(run.run_id)}<br>Сценарий: ${esc(run.scenario_id)}<br>Исполнено шагов: ${esc(record.steps_executed)} / ${esc(run.steps_total)}<br>Цель: ${esc(objectiveLabel(run.objective))}<br>Режим: ${esc(modeLabel(run.mode))}<br>Алгоритм: ${esc(record.run_metadata.algorithm)} ${esc(record.run_metadata.version)}</p><p>Показатели за весь выполненный период, включая историю до развилки. Выручка начисляется за полностью завершённые задания. Доля P3 при нулевом числе заданий с наступившим сроком не определена.</p><h2>Результат</h2><table><tbody>${tableRows}</tbody></table><h2>Состояние аппаратов на границе ${esc(run.step)}</h2><table><thead><tr><th>Аппарат</th><th>SOC, %</th><th>Температура, °C</th><th>Возраст калибровки</th><th>Доступен</th></tr></thead><tbody>${satelliteRows}</tbody></table><h2>Полученные сообщения</h2>${events ? `<table><thead><tr><th>ID</th><th>Шаг</th><th>Тип</th><th>Содержание</th></tr></thead><tbody>${events}</tbody></table>` : '<p>Сообщений нет.</p>'}<h2>Параметры и происхождение расчёта</h2><pre>${esc(JSON.stringify(record.run_metadata, null, 2))}</pre><p class="meta">Хеш исходного сценария: ${esc(record.initial_scenario_hash)}. Для машинного воспроизведения используйте JSON-выгрузку. HTML предназначен для чтения и печати.</p></body></html>`
}
