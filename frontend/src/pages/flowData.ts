import { api } from '../api/client'

export type FlowStatus = 'completed' | 'in_progress' | 'waiting' | 'missed'
export const statuses: FlowStatus[] = ['in_progress', 'waiting', 'completed', 'missed']
export const statusNames: Record<FlowStatus, string> = { completed: 'Завершены', in_progress: 'В работе', waiting: 'Ожидают', missed: 'Просрочены' }
export interface FlowJob {
  job_id: string; kind: string; priority: number; value_usd: number
  release_step: number; deadline_step: number; work_steps: number; remaining_steps: number
  progress: number; completed_step: number | null; eligible_satellites: string[]; status: FlowStatus
}
export interface FlowData { step: number; counts: Record<FlowStatus, number>; rows: FlowJob[]; reconstructed: boolean }
interface RawJob { id: string; kind: string; priority: number; value_usd: number; release_step: number; deadline_step: number; work_steps: number; eligible_satellites: string[] }
interface ExportRecord {
  steps_executed: number
  initial_scenario: { jobs: RawJob[]; satellites: { id: string }[] }
  events: { type: string; jobs?: RawJob[] }[]
  trace: { step: number; satellite_id: string; executed: string; requested: { job_id?: string }; completed_job?: string | null }[]
  summary: { jobs_total: number; jobs_completed: number }
}
const emptyCounts = (): Record<FlowStatus, number> => ({ completed: 0, in_progress: 0, waiting: 0, missed: 0 })

export function reconstructFlow(value: unknown): FlowData {
  const record = value as ExportRecord
  if (!record || !Array.isArray(record.initial_scenario?.jobs) || !Array.isArray(record.events) || !Array.isArray(record.trace) || !Number.isInteger(record.steps_executed)) throw new Error('В полной выгрузке нет данных для восстановления потока заданий.')
  const definitions = new Map<string, RawJob>()
  for (const job of record.initial_scenario.jobs) definitions.set(job.id, job)
  for (const event of record.events) if (event.type === 'add_jobs') for (const job of event.jobs ?? []) definitions.set(job.id, job)
  const work = new Map<string, number>(), completed = new Map<string, number>(), traceKeys = new Set<string>()
  for (const row of record.trace) {
    const key = `${row.step}:${row.satellite_id}`
    if (traceKeys.has(key) || row.step < 0 || row.step >= record.steps_executed) throw new Error('Журнал выгрузки содержит повторяющиеся или недопустимые шаги.')
    traceKeys.add(key)
    if (row.executed === 'job') {
      const id = row.requested.job_id
      if (!id || !definitions.has(id)) throw new Error('В журнале найдено неизвестное задание.')
      work.set(id, (work.get(id) ?? 0) + 1)
      if (row.completed_job) completed.set(row.completed_job, row.step + 1)
    }
  }
  if (record.trace.length !== record.steps_executed * record.initial_scenario.satellites.length || definitions.size !== record.summary.jobs_total) throw new Error('Выгрузка неполная: достоверно восстановить все задания нельзя. Обновите данные.')
  const counts = emptyCounts()
  const rows = [...definitions.values()].map((job): FlowJob => {
    const done = work.get(job.id) ?? 0
    if (done > job.work_steps || (done === job.work_steps) !== completed.has(job.id)) throw new Error('Прогресс задания не согласуется с журналом завершений.')
    const status: FlowStatus = completed.has(job.id) ? 'completed' : job.deadline_step <= record.steps_executed ? 'missed' : done > 0 ? 'in_progress' : 'waiting'
    counts[status]++
    return { ...job, job_id: job.id, status, remaining_steps: job.work_steps - done, progress: done / job.work_steps, completed_step: completed.get(job.id) ?? null }
  })
  if (counts.completed !== record.summary.jobs_completed) throw new Error('Число завершённых заданий не совпадает со сводкой выгрузки.')
  return { step: record.steps_executed, counts, rows, reconstructed: true }
}

export async function loadFlow(runId: string): Promise<FlowData> {
  const value = await api.flow(runId, { limit: 5000 }) as { step: number; counts: Record<FlowStatus, number>; jobs: Record<FlowStatus, Omit<FlowJob, 'status'>[]> }
  if (!value?.counts || !value.jobs) throw new Error('Сервер не вернул корзины потока заданий.')
  if (statuses.some(status => (value.jobs[status]?.length ?? 0) !== value.counts[status])) {
    // У /flow нет offset: только полный согласованный экспорт даёт пропущенные строки.
    return reconstructFlow(await api.exportRun(runId, true))
  }
  return { step: value.step, counts: value.counts, rows: statuses.flatMap(status => (value.jobs[status] ?? []).map(job => ({ ...job, status }))), reconstructed: false }
}
