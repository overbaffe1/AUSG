import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api/client'
import { useWorkspace } from './workspace'
import { ErrorNotice } from './components/common'
import ExplainDrawer from './components/ExplainDrawer'
import PreflightPage from './pages/PreflightPage'
import ShiftPage from './pages/ShiftPage'
import FlowPage from './pages/FlowPage'
import ConstellationPage from './pages/ConstellationPage'
import ComparePage from './pages/ComparePage'
import AnalyticsPage from './pages/AnalyticsPage'
import ExportPage from './pages/ExportPage'

const NAV = [
  { to: '/preflight', label: 'Предполётная оценка', icon: '◈' },
  { to: '/shift', label: 'Пульт смены', icon: '⌘' },
  { to: '/flow', label: 'Поток заданий', icon: '≡' },
  { to: '/constellation', label: 'Группировка', icon: '✧' },
  { to: '/compare', label: 'Сравнение', icon: '⇄' },
  { to: '/analytics', label: 'Аналитика', icon: '▥' },
  { to: '/export', label: 'Выгрузка', icon: '↗' },
]
interface RunItem { run_id: string; label: string; step: number; scenario_id: string }
export default function App() {
  const { run, selectRun, busy, status, error, clearError, task, refreshRun } = useWorkspace()
  const [runs, setRuns] = useState<RunItem[]>([])
  const [variant, setVariant] = useState(() => { try { return localStorage.getItem('kosmos-ui') ?? 'mission' } catch { return 'mission' } })
  useEffect(() => {
    document.documentElement.dataset.variant = variant
    try { localStorage.setItem('kosmos-ui', variant) } catch { /* Тема работает без хранилища. */ }
  }, [variant])
  useEffect(() => {
    if (busy) return
    let active = true
    void api.runs().then(r => { if (active) setRuns(r.runs as RunItem[]) }).catch(() => { /* Ошибки рабочих запросов отображаются в контексте. */ })
    return () => { active = false }
  }, [busy, run?.run_id, run?.step])
  return <div className="layout">
    <aside className="sidebar">
      <NavLink to="/preflight" className="brand"><span className="brand-mark" aria-hidden="true">✳</span><span><strong>КОСМОС</strong><small>Управление группировкой</small></span></NavLink>
      <div className="nav-caption">РАБОЧЕЕ ПРОСТРАНСТВО</div>
      <nav aria-label="Основная навигация">{NAV.map((item, i) => <NavLink key={item.to} to={item.to} className={({ isActive }) => isActive ? 'active' : ''}><span className="nav-icon" aria-hidden="true">{item.icon}</span><span>{item.label}</span><small>{String(i + 1).padStart(2, '0')}</small></NavLink>)}</nav>
      <div className="sidebar-foot"><span className="status-dot" /> Автономное планирование<small>Оператор задаёт цель.<br />Система выбирает действия.</small></div>
    </aside>
    <div className="main-column">
      <header className="topbar">
        <div className="run-picker"><span className="overline">ТЕКУЩИЙ РАСЧЁТ</span><select aria-label="Текущий расчёт" value={run?.run_id ?? ''} disabled={busy} onChange={e => { const id = e.target.value; void task('Открываем расчёт…', async () => selectRun(id ? await api.run(id) : null)) }}><option value="">Новая смена</option>{runs.map(r => <option key={r.run_id} value={r.run_id}>{r.label || r.scenario_id} · {r.step} · {r.run_id.slice(0, 6)}</option>)}{run && !runs.some(r => r.run_id === run.run_id) && <option value={run.run_id}>{run.label || run.scenario_id}</option>}</select></div>
        <div className="topbar-right">{run && <button className="ghost" aria-label="Обновить текущий расчёт" title="Обновить текущий расчёт" disabled={busy} onClick={()=>void task("Обновляем состояние…",refreshRun)}>↻</button>}{run && <span className="step-chip">Шаг <b>{run.step}</b> / {run.steps_total}</span>}<label className="variant-picker"><span>Вариант UI</span><select aria-label="Вариант интерфейса" value={variant} onChange={e => setVariant(e.target.value)}><option value="mission">01 · Пульт</option><option value="daylight">02 · Аналитик</option><option value="compact">03 · Диспетчер</option></select></label></div>
      </header>
      <main className="content">
        {busy && <div className="operation-status" role="status"><span className="spinner" />{status}</div>}
        {!!error && <div className="global-error"><ErrorNotice error={error} /><button className="ghost" onClick={clearError}>Закрыть</button></div>}
        <Routes>
          <Route path="/" element={<Navigate to="/preflight" replace />} />
          <Route path="/preflight" element={<PreflightPage />} />
          <Route path="/shift" element={<ShiftPage />} />
          <Route path="/flow" element={<FlowPage />} />
          <Route path="/constellation" element={<ConstellationPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/export" element={<ExportPage />} />
          <Route path="*" element={<Navigate to="/preflight" replace />} />
        </Routes>
        <footer className="page-footer"><span>КОСМОС / Консоль оператора</span><span>Автономное управление потоком заданий</span></footer>
      </main>
    </div>
    <ExplainDrawer />
  </div>
}

