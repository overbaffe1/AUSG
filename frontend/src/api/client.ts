// Клиент API. Типы держим в одном месте — это общая граница с бэкендом.
// Менять синхронно с backend/app/schemas.py и docs/api.md.

export type Objective = 'priority_service' | 'commercial'
export type Mode = 'centralized' | 'federated'

export interface ApiError {
  code: string
  message: string
  details?: Record<string, unknown>
}

export class ServiceError extends Error {
  code: string
  details: Record<string, unknown>
  constructor(error: ApiError) {
    super(error.message)
    this.code = error.code
    this.details = error.details ?? {}
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const text = await response.text()
  let payload: unknown
  try { payload = text ? JSON.parse(text) : null } catch {
    throw new ServiceError({ code: 'HTTP_' + response.status, message: 'Сервер вернул ответ не в формате JSON. Проверьте доступность API.', details: { status: response.status } })
  }
  if (!response.ok) {
    const failure = payload as { error?: ApiError; detail?: unknown } | null
    const error = failure?.error ?? {
      code: 'HTTP_' + response.status,
      message: response.status === 422 ? 'Проверьте поля запроса: сервер отклонил данные.' : response.statusText || 'Запрос отклонён сервером',
      details: { validation: failure?.detail },
    }
    throw new ServiceError(error)
  }
  return payload as T
}

const get = <T,>(path: string) => request<T>(path)
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

// ------------------------------------------------------------------ сценарии

export interface ScenarioSummary {
  id: string
  title: string
  source: string
  steps: number
  duration_hours: number
  satellites: number
  jobs_total: number
  jobs_by_kind: Record<string, number>
  jobs_by_priority: Record<string, number>
  value_total_usd: number
}

export interface Preflight {
  scenario_id: string
  steps: number
  satellites: number
  jobs_total: number
  ground_link: {
    steps_with_single_gateway: number
    single_gateway_share: number
    blind_steps: number[]
    longest_gap_steps: number
    contacts_histogram: Record<string, number>
  }
  capacity: Record<string, { demand_steps: number; capacity_steps: number; oversubscription: number | null }>
  structural_infeasible: { count: number; value_usd: number; jobs: unknown[]; truncated?: boolean }
  starved_satellites: { satellite_id: string; demand_steps: number; contact_steps: number }[]
  energy_short_satellites: { satellite_id: string; budget_wh: number; demand_wh: number }[]
  money: {
    total_usd: number
    priority3_usd: number
    priority3_jobs: number
    reachable_ceiling_usd: number
  }
}

// ------------------------------------------------------------------ прогоны

export interface SatelliteState {
  energy_wh: number
  soc_pct: number
  temp_c: number
  calibration_age_steps: number
  calibration_valid_for: number
  available: boolean
  in_contact: boolean
}

export interface RunState {
  run_id: string
  label: string
  scenario_id: string
  step: number
  steps_total: number
  finished: boolean
  objective: Objective
  mode: Mode
  planner: string
  state_digest: string
  satellites: Record<string, SatelliteState>
  summary: Record<string, number>
}

export const api = {
  health: () => get<{ status: string; version: string }>('/api/health'),

  scenarios: () => get<{ scenarios: ScenarioSummary[] }>('/api/scenarios'),
  scenario: (id: string) => get<ScenarioSummary>(`/api/scenarios/${id}`),
  preflight: (id: string) => get<Preflight>(`/api/scenarios/${id}/preflight`),
  jobCost: (id: string, jobId: string) =>
    get<{ job: unknown; cost_cards: unknown[] }>(`/api/scenarios/${id}/jobs/${jobId}/cost`),
  derive: (id: string, body: unknown) =>
    post<ScenarioSummary>(`/api/scenarios/${id}/derive`, body),
  uploadScenario: (scenario: unknown) =>
    post<ScenarioSummary>('/api/scenarios/upload', scenario),

  runs: () => get<{ runs: unknown[] }>('/api/runs'),
  createRun: (body: {
    scenario_id: string
    objective?: Objective
    mode?: Mode
    planner?: string
    label?: string
  }) => post<RunState>('/api/runs', body),
  run: (runId: string) => get<RunState>(`/api/runs/${runId}`),
  advance: (runId: string, body: { steps?: number; to_step?: number; to_end?: boolean }) =>
    post<{ executed_rows: number; state: RunState }>(`/api/runs/${runId}/advance`, body),
  setObjective: (runId: string, objective: Objective) =>
    post<unknown>(`/api/runs/${runId}/objective`, { objective }),

  // Жюри подаёт ОДИН объект события без обёртки — это основной путь.
  sendEvent: (runId: string, event: unknown) =>
    post<unknown>(`/api/runs/${runId}/events`, event),
  sendManualEvent: (
    runId: string,
    body: { type: 'satellite_outage' | 'close_downlink'; satellite_ids: string[]; end_step: number },
  ) => post<unknown>(`/api/runs/${runId}/events/manual`, body),

  flow: (runId: string, params: Record<string, string | number> = {}) => get<unknown>(`/api/runs/${runId}/flow?` + new URLSearchParams(params as never)),
  trace: (runId: string, params: Record<string, string | number> = {}) =>
    get<unknown>(`/api/runs/${runId}/trace?` + new URLSearchParams(params as never)),
  // full=true восстанавливает глубокую часть повтором прогона (до 5 с на
  // P04_demand). Вызывать только по явному запросу оператора: критерий О3
  // требует, чтобы панель разбора открывалась за 300 мс.
  explain: (runId: string, step: number, satelliteId: string, full = false) =>
    get<unknown>(`/api/runs/${runId}/explain?step=${step}&satellite_id=${satelliteId}${full ? '&full=true' : ''}`),
  analytics: (runId: string) => get<unknown>(`/api/runs/${runId}/analytics`),
  bundles: (runId: string) => get<unknown>(`/api/runs/${runId}/bundles`),

  branch: (runId: string, body: unknown) => post<RunState>(`/api/runs/${runId}/branch`, body),
  compare: (a: string, b: string) => get<unknown>(`/api/compare?a=${a}&b=${b}`),

  exportRun: (runId: string, includeTrace = true) =>
    get<unknown>(`/api/runs/${runId}/export?include_trace=${includeTrace}`),
  importRun: (result: unknown) => post<unknown>('/api/runs/import', result),
  exportRunText: async (runId: string, includeTrace = true): Promise<string> => {
    const response = await fetch(`/api/runs/${runId}/export?include_trace=${includeTrace}`)
    const text = await response.text()
    if (!response.ok) {
      let message = 'Не удалось сохранить расчёт'
      try { message = JSON.parse(text).error?.message ?? message } catch { /* Оставляем понятное сообщение. */ }
      throw new ServiceError({ code: 'HTTP_' + response.status, message })
    }
    return text
  },
  importRunText: (text: string) => request<unknown>('/api/runs/import', { method: 'POST', body: text.replace(/^\uFEFF/, '') }),
}
