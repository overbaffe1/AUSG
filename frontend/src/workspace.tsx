import { createContext, useContext, useEffect, useRef, useState, ReactNode } from 'react'
import { api, RunState, ScenarioSummary } from './api/client'

interface Workspace {
  run: RunState | null
  selectRun: (run: RunState | null) => void
  refreshRun: () => Promise<void>
  scenarios: ScenarioSummary[]
  reloadScenarios: () => Promise<void>
  scenarioId: string
  setScenarioId: (id: string) => void
  busy: boolean
  status: string
  error: unknown
  clearError: () => void
  task: (label: string, action: () => Promise<void>) => Promise<void>
  decision: { runId: string; satelliteId: string; step: number } | null
  openDecision: (satelliteId: string, step: number) => void
  closeDecision: () => void
}
const Context = createContext<Workspace | null>(null)
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [run, setRun] = useState<RunState | null>(null)
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([])
  const [scenarioId, setScenarioId] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [decision, setDecision] = useState<Workspace['decision']>(null)
  const lock = useRef(false)
  const currentId = useRef<string | null>(null)
  const selectRun = (value: RunState | null) => {
    if (currentId.current !== value?.run_id) setDecision(null)
    currentId.current = value?.run_id ?? null
    setRun(value)
    try { value ? localStorage.setItem('kosmos-run', value.run_id) : localStorage.removeItem('kosmos-run') } catch { /* Хранилище браузера может быть отключено. */ }
  }
  const reloadScenarios = async () => {
    const response = await api.scenarios()
    setScenarios(response.scenarios)
    setScenarioId(previous => response.scenarios.some(s => s.id === previous) ? previous : response.scenarios[0]?.id ?? '')
  }
  const refreshRun = async () => {
    const id = currentId.current
    if (!id) return
    const value = await api.run(id)
    if (currentId.current === id) selectRun(value)
  }
  const task = async (label: string, action: () => Promise<void>) => {
    if (lock.current) return
    lock.current = true; setBusy(true); setStatus(label); setError(null)
    try { await action() } catch (failure) { setError(failure) }
    finally { lock.current = false; setBusy(false); setStatus('') }
  }
  useEffect(() => {
    let active = true
    void reloadScenarios().catch(setError)
    let saved: string | null = null
    try { saved = localStorage.getItem('kosmos-run') } catch { /* Можно работать без восстановления. */ }
    if (saved) void api.run(saved).then(value => { if (active && currentId.current === null) selectRun(value) }).catch(() => { if (active) { try { localStorage.removeItem('kosmos-run') } catch { /* Не блокируем запуск. */ } } })
    return () => { active = false }
  }, [])
  return <Context.Provider value={{ run, selectRun, refreshRun, scenarios, reloadScenarios, scenarioId, setScenarioId, busy, status, error, clearError: () => setError(null), task, decision, openDecision: (satelliteId, step) => { if (run) setDecision({ runId: run.run_id, satelliteId, step }) }, closeDecision: () => setDecision(null) }}>{children}</Context.Provider>
}
export function useWorkspace() {
  const value = useContext(Context)
  if (!value) throw new Error('Рабочее пространство не подключено')
  return value
}
