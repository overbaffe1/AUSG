import { useEffect, useState } from 'react'
import { api, RunState } from '../api/client'

export interface TraceRow {
  step: number; satellite_id: string; requested: { action: string; job_id?: string }; executed: string; reason: string
  energy_before_wh: number; energy_after_wh: number; temp_before_c: number; temp_after_c: number
  solar_w: number; load_w: number; heater_w: number; calibration_age_steps: number
  completed_job: string | null; brownout: boolean; below_reserve: boolean
}
export interface ScenarioJob { id: string; kind: 'relay' | 'downlink'; priority: number; deadline_step: number; value_usd: number }
export interface InitialScenario {
  model: Record<string, number>
  time: { steps: number; step_s: number }
  satellites: { id: string; capacity_wh: number; initial_soc_pct: number; initial_temp_c: number }[]
  jobs: ScenarioJob[]
  environment: Record<string, { downlink_available: boolean[]; relay_available: boolean[] }>
  failures: { satellite_id: string; start_step: number; end_step: number }[]
}
interface ReceivedEvent { type: string; at_step: number; end_step?: number; satellite_ids?: string[]; jobs?: ScenarioJob[] }
export interface CompactExport { initial_scenario: InitialScenario; events: ReceivedEvent[]; steps_executed: number }
export interface BundleItem { job_id: string; priority_rank: number; earliest_step: number; latest_start_step: number; min_soc_pct: number; backup_rank: number; reserved_steps: number[] | null }
export interface FlightBundle { satellite_id: string; issued_at_step: number; valid_until_step: number; gateway_steps: number[]; fallback: string; queue: BundleItem[] }
export interface BundleSnapshot { mode: string; bundles: FlightBundle[]; note?: string; replan_gap?: number; horizon_steps?: number; policy?: string; uplink?: { uploads: { step: number; gateway: string | null; satellites: string[]; horizon_steps: number }[] } }
export interface Analytics {
  run_id: string; step: number; summary: Record<string, number>
  priority_service: { critical_jobs_due: number; critical_jobs_completed_on_time: number; share: number | null }
  utilisation: Record<string, { job_steps: number; calibrate_steps: number; idle_steps: number; executed_steps: number; job_share: number | null }>
  blocked_reasons: Record<string, number>; idle_reasons: Record<string, number>
  ground_link: { limit_per_step: number; slots_available: number; slots_used: number; utilisation: number | null; saturated_steps: number }
  lost_work: { work_steps_in_missed_jobs: number; jobs_due_missed: number }
  energy: { below_reserve_satellite_steps: number; critical_soc_satellite_steps: number; brownout_satellite_steps: number; minimum_soc_pct: number }
}
export interface OperationData { rows: TraceRow[]; exported: CompactExport; analytics: Analytics; bundles: BundleSnapshot; jobs: Map<string, ScenarioJob> }

// to_step включителен, offset в API отсутствует. Окна не пересекаются и не превышают лимит 5000 строк.
export async function loadCompleteTrace(runId: string, steps: number, satellites: number): Promise<TraceRow[]> {
  const windowSize = Math.max(1, Math.min(96, Math.floor(4800 / Math.max(1, satellites))))
  const rows: TraceRow[] = []
  for (let from = 0; from < steps; from += windowSize) {
    const to = Math.min(steps - 1, from + windowSize - 1)
    const response = await api.trace(runId, { from_step: from, to_step: to, limit: 5000 }) as { rows: TraceRow[] }
    const expected = (to - from + 1) * satellites
    if (!Array.isArray(response.rows) || response.rows.length !== expected) throw new Error(`Неполный журнал за шаги ${from}–${to}: ожидается ${expected} строк. Обновите данные после завершения операции.`)
    rows.push(...response.rows)
  }
  return rows
}

export function useOperationData(run: RunState | null) {
  const [result, setResult] = useState<{ run: RunState; data: OperationData } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    if (!run) { setResult(null); return }
    let cancelled = false
    setLoading(true); setError(null)
    void Promise.all([
      loadCompleteTrace(run.run_id, run.step, Object.keys(run.satellites).length),
      api.exportRun(run.run_id, false), api.analytics(run.run_id), api.bundles(run.run_id),
    ]).then(([rows, rawExport, rawAnalytics, rawBundles]) => {
      if (cancelled) return
      const exported = rawExport as CompactExport, analytics = rawAnalytics as Analytics, bundles = rawBundles as BundleSnapshot
      if (!exported.initial_scenario?.model || !Array.isArray(exported.initial_scenario?.satellites)) throw new Error('В выгрузке отсутствуют параметры исходного сценария.')
      if (exported.steps_executed !== run.step || analytics.step !== run.step) throw new Error('Расчёт продвинулся во время загрузки. Обновите состояние смены и повторите запрос.')
      const jobs = new Map(exported.initial_scenario.jobs.map(job => [job.id, job]))
      for (const event of exported.events) for (const job of event.jobs ?? []) jobs.set(job.id, job)
      setResult({ run, data: { rows, exported, analytics, bundles, jobs } })
    }).catch(failure => { if (!cancelled) setError(failure) }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [run, revision])
  return { data: result?.run === run ? result.data : null, loading, error, reload: () => setRevision(value => value + 1) }
}

export const operationNames: Record<string, string> = { idle: 'Ожидание', calibrate: 'Калибровка', relay: 'Ретрансляция', downlink: 'Сброс данных', job: 'Задание' }
export const reasonNames: Record<string, string> = {
  accepted: 'Принято', idle: 'Ожидание', no_eligible_job: 'Нет доступного задания', energy_recovery: 'Восстановление заряда', thermal_hold: 'Температурная пауза',
  ground_capacity_reserved: 'Наземный слот зарезервирован', gateway_hold: 'Сохранение связи с Землёй', gateway_protected: 'Защита единственного шлюза',
  calibration_required: 'Нужна калибровка', calibration_preventive: 'Профилактическая калибровка', satellite_unavailable: 'Аппарат недоступен',
  infeasible_by_contacts: 'Не хватает контактов', infeasible_by_deadline: 'Не хватает времени до срока', negative_value: 'Ресурс уступает более ценному применению',
  no_completion_margin: 'Недостаточный запас до срока', energy_forecast_short: 'Недостаточный прогноз заряда', preempted_by: 'Вытеснение', rejected_by_model: 'Отказ модели',
  energy_reserve: 'Энергетический резерв', thermal_limit: 'Температурный предел', no_contact: 'Нет контакта', ground_capacity: 'Лимит наземного сегмента',
}
export const rowOperation = (row: TraceRow, jobs: Map<string, ScenarioJob>) => row.executed === 'job' ? jobs.get(row.requested.job_id ?? '')?.kind ?? 'job' : row.executed

export function contactHistory(data: OperationData) {
  const s = data.exported.initial_scenario, events = data.exported.events
  const fails = [...(s.failures ?? []), ...events.filter(e => e.type === 'satellite_outage').flatMap(e => (e.satellite_ids ?? []).map(id => ({ satellite_id: id, start_step: e.at_step, end_step: e.end_step ?? e.at_step })))]
  const closures = events.filter(e => e.type === 'close_downlink')
  const downlink: number[] = [], any: number[] = []
  for (let step = 0; step < s.time.steps; step++) {
    let ground = 0, sessions = 0
    for (const sat of s.satellites) {
      if (fails.some(f => f.satellite_id === sat.id && f.start_step <= step && step < f.end_step)) continue
      const env = s.environment[sat.id]
      const dl = env.downlink_available[step] && !closures.some(e => e.satellite_ids?.includes(sat.id) && e.at_step <= step && step < (e.end_step ?? 0))
      ground += Number(dl); sessions += Number(dl || env.relay_available[step])
    }
    downlink.push(ground); any.push(sessions)
  }
  return { downlink, any }
}
