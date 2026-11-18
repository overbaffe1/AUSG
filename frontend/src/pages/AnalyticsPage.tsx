import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { EmptyRun, ErrorNotice, Kpi, Loading, money, num, PageHeading, percent } from '../components/common'
import { useWorkspace } from '../workspace'
import { OperationData, reasonNames, TraceRow, useOperationData } from './operationalData'
import { OperationMatrix, TraceTable } from './OperationalVisuals'
import './operational.css'

type TraceFilter = { kind: 'all' | 'job' | 'completed' | 'p3' | 'blocked' | 'idle' | 'calibrate' | 'reserve' | 'critical' | 'brownout' | 'lost' | 'downlink' | 'satellite' | 'step'; value?: string | number; label: string }
interface ReasonSearch { code: string; checked: number; total: number; rows: TraceRow[]; running: boolean; error?: unknown }
function filterRows(data: OperationData, filter: TraceFilter, currentStep: number) {
  const capacities = new Map(data.exported.initial_scenario.satellites.map(sat => [sat.id, sat.capacity_wh]))
  const completed = new Set(data.rows.flatMap(row => row.completed_job ? [row.completed_job] : []))
  return data.rows.filter(row => {
    const job = data.jobs.get(row.requested.job_id ?? '')
    switch (filter.kind) {
      case 'job': return row.executed === 'job'
      case 'completed': return Boolean(row.completed_job)
      case 'p3': return row.executed === 'job' && job?.priority === 3
      case 'blocked': return filter.value ? row.reason === filter.value : !['accepted', 'idle'].includes(row.reason)
      case 'idle': return row.executed === 'idle'
      case 'calibrate': return row.executed === 'calibrate'
      case 'reserve': return row.below_reserve
      case 'critical': return row.energy_after_wh / (capacities.get(row.satellite_id) ?? Infinity) * 100 < data.exported.initial_scenario.model.critical_soc_pct
      case 'brownout': return row.brownout
      case 'lost': return row.executed === 'job' && Boolean(job && job.deadline_step <= currentStep && !completed.has(job.id))
      case 'downlink': return row.executed === 'job' && job?.kind === 'downlink'
      case 'satellite': return row.satellite_id === filter.value
      case 'step': return row.step === filter.value
      default: return true
    }
  })
}

function GroundSlots({ data, steps, onStep }: { data: OperationData; steps: number; onStep: (step: number) => void }) {
  const counts = useMemo(() => {
    const values = Array<number>(steps).fill(0)
    data.rows.forEach(row => { if (row.executed === 'job' && data.jobs.get(row.requested.job_id ?? '')?.kind === 'downlink') values[row.step]++ })
    return values
  }, [data, steps])
  const limit = data.analytics.ground_link.limit_per_step
  const [chosenStep, setChosenStep] = useState(0)
  const width = Math.max(680, steps * 4 + 52), height = 176, left = 34, top = 22, plotH = 116, plotW = width - left - 13
  return <><div className="ops-ground-scroll"><svg viewBox={`0 0 ${width} ${height}`} style={{ minWidth: width }} role="img" aria-label={`Использование наземных слотов, лимит ${limit} на шаг`}><line x1={left} x2={width - 13} y1={top} y2={top} stroke="#e1ad66" strokeDasharray="5 4" /><text x={left} y={12}>Лимит: {limit}</text>{counts.map((count, step) => { const barW = plotW / Math.max(steps, 1), barH = count / Math.max(limit, 1) * plotH; return <g key={step} onClick={() => onStep(step)} style={{ cursor: 'pointer' }}><rect x={left + step * barW} y={top} width={Math.max(1, barW - 1)} height={plotH} fill="var(--panel-alt)" /><rect x={left + step * barW} y={top + plotH - barH} width={Math.max(1, barW - 1)} height={barH} fill={count >= limit ? '#e1ad66' : '#7698eb'} /><title>Шаг {step}: занято {count} / {limit}. Нажмите для журнала.</title></g> })}<text x={left} y={height - 14}>0</text><text x={width - 13} y={height - 14} textAnchor="end">шаг {Math.max(0, steps - 1)}</text></svg></div><div className="toolbar ops-compact-toolbar"><span className="muted">Нажатие столбца открывает журнал этого шага.</span><label className="field">Шаг<input type="number" min={0} max={Math.max(0, steps - 1)} value={chosenStep} onChange={e => setChosenStep(Number(e.target.value))} /></label><button className="button secondary" disabled={!steps || !Number.isInteger(chosenStep) || chosenStep < 0 || chosenStep >= steps} onClick={() => onStep(chosenStep)}>Показать операции</button></div></>
}

export default function AnalyticsPage() {
  const { run, openDecision } = useWorkspace()
  const { data, loading, error, reload } = useOperationData(run)
  const [filter, setFilter] = useState<TraceFilter>({ kind: 'all', label: 'Все операции' })
  const [reasonSearch, setReasonSearch] = useState<ReasonSearch | null>(null)
  const scanToken = useRef(0)
  const traceAnchor = useRef<HTMLDivElement>(null)
  const satellites = useMemo(() => Object.keys(run?.satellites ?? {}).sort(), [run])
  const filteredRows = useMemo(() => data && run ? filterRows(data, filter, run.step) : [], [data, filter, run])
  useEffect(() => { scanToken.current++; setReasonSearch(null); setFilter({ kind: 'all', label: 'Все операции' }); return () => { scanToken.current++ } }, [run])
  function showRows(value: TraceFilter) { scanToken.current++; setReasonSearch(null); setFilter(value); setTimeout(() => traceAnchor.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0) }

  async function searchIdleReason(code: string, resume = false) {
    if (!data || !run) return
    const candidates = data.rows.filter(row => row.requested.action === 'idle')
    const previous = resume && reasonSearch?.code === code ? reasonSearch : null
    const start = previous?.checked ?? 0, stop = Math.min(candidates.length, start + 96)
    const matches = [...(previous?.rows ?? [])], token = ++scanToken.current, id = run.run_id
    setReasonSearch({ code, checked: start, total: candidates.length, rows: matches, running: true })
    setTimeout(() => traceAnchor.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
    try {
      // В trace есть только причина модели. Причину планировщика подтверждаем explain,
      // не приписываем каждому idle выбранную причину из агрегированного счётчика.
      for (let offset = start; offset < stop; offset += 4) {
        if (token !== scanToken.current) return
        const batch = candidates.slice(offset, Math.min(stop, offset + 4))
        const explanations = await Promise.all(batch.map(row => api.explain(id, row.step, row.satellite_id) as Promise<{ reason_code?: string }>))
        if (token !== scanToken.current) return
        batch.forEach((row, index) => { if (explanations[index].reason_code === code) matches.push(row) })
        setReasonSearch({ code, checked: offset + batch.length, total: candidates.length, rows: [...matches], running: offset + batch.length < stop })
      }
    } catch (failure) { if (token === scanToken.current) setReasonSearch(value => value ? { ...value, running: false, error: failure } : null) }
  }
  if (!run) return <EmptyRun />
  if (!data) return <div className="ops-page"><PageHeading title="Аналитика смены" subtitle="Показатели исполнения, причины и проверяемые операции." /><ErrorNotice error={error} />{loading ? <Loading text="Загружаем аналитику и полную историю операций…" /> : <button className="button" onClick={reload}>Повторить загрузку</button>}</div>
  const a = data.analytics, summary = a.summary
  const totals = Object.values(a.utilisation).reduce((sum, sat) => ({ job: sum.job + sat.job_steps, idle: sum.idle + sat.idle_steps, calibrate: sum.calibrate + sat.calibrate_steps, total: sum.total + sat.executed_steps }), { job: 0, idle: 0, calibrate: 0, total: 0 })
  const blockedEntries = Object.entries(a.blocked_reasons), idleEntries = Object.entries(a.idle_reasons)
  const maxBlocked = Math.max(1, ...blockedEntries.map(([, count]) => count)), maxIdle = Math.max(1, ...idleEntries.map(([, count]) => count))
  return <div className="ops-page"><PageHeading title="Аналитика смены" subtitle={`${run.label || run.scenario_id} · исполнено ${run.step} / ${run.steps_total} шагов · ${num(data.rows.length)} аппарат-шагов`} actions={<button className="button secondary" disabled={loading} onClick={reload}>Обновить данные</button>} /><ErrorNotice error={error} />
    <div className="kpi-grid"><Kpi label="Выручка" value={money(summary.revenue_usd)} note="Только полностью завершённые задания" onClick={() => showRows({ kind: 'completed', label: 'Операции завершения оплаченных заданий' })} /><Kpi label="Приоритет 3 в срок" value={percent(a.priority_service.share)} note={a.priority_service.critical_jobs_due ? `${a.priority_service.critical_jobs_completed_on_time} / ${a.priority_service.critical_jobs_due} с наступившим сроком` : 'Сроки P3 ещё не наступили'} onClick={() => showRows({ kind: 'p3', label: 'Исполненные операции заданий P3' })} /><Kpi label="Завершено заданий" value={`${num(summary.jobs_completed)} / ${num(summary.jobs_total)}`} note="Все известные задания остаются в знаменателе" onClick={() => showRows({ kind: 'completed', label: 'Завершающие операции' })} /><Kpi label="Полезная загрузка" value={percent(totals.total ? totals.job / totals.total : null)} note={`${num(totals.job)} аппарат-шагов выполнения заданий`} onClick={() => showRows({ kind: 'job', label: 'Полезная работа · только исполненные job' })} /></div>
    <div className="ops-two-column"><section className="panel"><div className="ops-panel-heading"><div><h3>Распределение времени</h3><p className="muted">Ожидание и калибровка учитываются отдельно от полезной работы.</p></div></div><div className="ops-time-bar" aria-label="Распределение аппарат-шагов">{[{ key: 'job', value: totals.job, color: '#63c5ad', label: 'Задания' }, { key: 'calibrate', value: totals.calibrate, color: '#dcb169', label: 'Калибровка' }, { key: 'idle', value: totals.idle, color: '#566d85', label: 'Ожидание' }].map(part => <button key={part.key} title={`${part.label}: ${part.value}`} style={{ width: `${totals.total ? part.value / totals.total * 100 : 0}%`, background: part.color }} disabled={!part.value} onClick={() => showRows({ kind: part.key as 'job' | 'calibrate' | 'idle', label: part.label })} />)}</div><div className="ops-stat-list">{[['job', 'Полезная работа', totals.job], ['calibrate', 'Калибровка', totals.calibrate], ['idle', 'Ожидание', totals.idle]].map(([key, label, value]) => <button key={key} onClick={() => showRows({ kind: key as 'job' | 'calibrate' | 'idle', label: String(label) })}><span>{label}</span><strong>{num(value)} <small>{percent(totals.total ? Number(value) / totals.total : null)}</small></strong></button>)}</div></section><section className="panel"><div className="ops-panel-heading"><div><h3>Энергия и потраченная работа</h3><p className="muted">Нажмите показатель для перехода к операциям.</p></div></div><div className="ops-stat-list"><button onClick={() => showRows({ kind: 'reserve', label: 'Аппарат-шаги ниже резерва' })}><span>Ниже резервного SOC</span><strong>{num(a.energy.below_reserve_satellite_steps)}</strong></button><button onClick={() => showRows({ kind: 'critical', label: 'Аппарат-шаги ниже критического SOC' })}><span>Ниже критического SOC</span><strong>{num(a.energy.critical_soc_satellite_steps)}</strong></button><button onClick={() => showRows({ kind: 'brownout', label: 'Обесточивания' })}><span>Обесточивания</span><strong>{num(a.energy.brownout_satellite_steps)}</strong></button><button onClick={() => showRows({ kind: 'lost', label: 'Работа в просроченных незавершённых заданиях' })}><span>Работа в просроченных заданиях</span><strong>{num(a.lost_work.work_steps_in_missed_jobs)}</strong></button></div><p className="muted ops-footnote">Минимум SOC: {num(a.energy.minimum_soc_pct, 1)} %. Просрочено заданий: {num(a.lost_work.jobs_due_missed)}. Энергетический дефицит возможен и при ожидании из-за условий сценария.</p></section></div>
    <div className="ops-two-column"><section className="panel"><div className="ops-panel-heading"><div><h3>Отказы модели</h3><p className="muted">Причины отклонения запрошенной операции.</p></div><span className="badge">{num(blockedEntries.reduce((sum, [, value]) => sum + value, 0))}</span></div>{blockedEntries.length ? <div className="ops-reason-bars">{blockedEntries.map(([code, count]) => <button key={code} onClick={() => showRows({ kind: 'blocked', value: code, label: `Отказ модели · ${reasonNames[code] ?? code}` })}><span>{reasonNames[code] ?? code}</span><strong>{num(count)}</strong><i style={{ width: `${count / maxBlocked * 100}%`, background: '#ce7786' }} /></button>)}</div> : <p className="empty">Модель не отклоняла команды за выполненные шаги.</p>}</section><section className="panel"><div className="ops-panel-heading"><div><h3>Причины ожидания</h3><p className="muted">Решения планировщика, не причины срыва задания.</p></div></div>{idleEntries.length ? <div className="ops-reason-bars">{idleEntries.map(([code, count]) => <button key={code} onClick={() => void searchIdleReason(code)}><span>{reasonNames[code] ?? code}</span><strong>{num(count)}</strong><i style={{ width: `${count / maxIdle * 100}%`, background: '#7297ca' }} /></button>)}</div> : <p className="empty">Решений об ожидании пока нет.</p>}</section></div>
    <section className="panel"><div className="ops-panel-heading"><div><h3>Загрузка аппаратов</h3><p className="muted">Полезная доля — только выполнение заданий. Нажмите карточку для журнала аппарата.</p></div></div><div className="ops-utilisation-grid">{satellites.map(sid => { const usage = a.utilisation[sid], share = usage?.job_share; return <button key={sid} onClick={() => showRows({ kind: 'satellite', value: sid, label: `${sid} · все операции` })} style={{ background: share === null || share === undefined ? 'transparent' : `rgba(73, 183, 157, ${.05 + share * .4})` }}><strong>{sid}</strong><b>{percent(share)}</b><span>{usage?.job_steps ?? 0} работа · {usage?.calibrate_steps ?? 0} кал. · {usage?.idle_steps ?? 0} ожид.</span></button> })}</div><details className="ops-bundle-details"><summary>Матрица фактических действий</summary><OperationMatrix rows={data.rows} satellites={satellites} steps={run.step} totalSteps={run.steps_total} jobs={data.jobs} onSelect={openDecision} /></details></section>
    <section className="panel"><div className="ops-panel-heading"><div><h3>Наземный сегмент по шагам</h3><p className="muted">Только фактически исполненные задания сброса данных; лимит взят из сценария.</p></div><button className="ops-metric-link" onClick={() => showRows({ kind: 'downlink', label: 'Исполненные сбросы данных' })}>{num(a.ground_link.slots_used)} / {num(a.ground_link.slots_available)} слотов · {percent(a.ground_link.utilisation)}</button></div>{run.step ? <GroundSlots data={data} steps={run.step} onStep={step => showRows({ kind: 'step', value: step, label: `Шаг ${step} · все аппараты` })} /> : <p className="empty">График появится после первого выполненного шага.</p>}<p className="muted ops-footnote">Полностью занят лимит на {num(a.ground_link.saturated_steps)} шагах. Ёмкость {num(a.ground_link.slots_available)} — глобальный лимит × исполненные шаги; наличие контактных окон дополнительно ограничивает использование.</p></section>
    <div ref={traceAnchor} className="ops-trace-anchor">{reasonSearch && <section className="panel"><div className="ops-panel-heading"><div><h3>Проверка причины ожидания</h3><p className="muted">{reasonNames[reasonSearch.code] ?? reasonSearch.code}. Проверено {num(reasonSearch.checked)} / {num(reasonSearch.total)} решений; найдено {num(reasonSearch.rows.length)}.</p></div></div><p className="muted">В журнале модели нет кода причины планировщика, поэтому совпадения проверяются по объяснениям. Каждая порция — до 96 решений; найденные строки ниже подтверждены сервером.</p><ErrorNotice error={reasonSearch.error} /><div className="toolbar">{reasonSearch.running ? <><Loading text="Проверяем объяснения…" /><button className="button secondary" onClick={() => { scanToken.current++; setReasonSearch(previous => previous ? { ...previous, running: false } : null) }}>Остановить поиск</button></> : <button className="button secondary" disabled={reasonSearch.checked >= reasonSearch.total} onClick={() => void searchIdleReason(reasonSearch.code, true)}>Проверить следующую порцию</button>}<button className="button secondary" onClick={() => showRows({ kind: 'all', label: 'Все операции' })}>Снять фильтр</button></div></section>}{!reasonSearch && <div className="toolbar"><label className="field">Журнал<select value={filter.kind === 'satellite' || filter.kind === 'step' || (filter.kind === 'blocked' && filter.value) ? 'custom' : filter.kind} onChange={e => showRows({ kind: e.target.value as TraceFilter['kind'], label: e.target.selectedOptions[0].text })}><option value="all">Все операции</option><option value="job">Полезная работа</option><option value="completed">Завершение заданий</option><option value="p3">Операции P3</option><option value="blocked">Отказы модели</option><option value="idle">Ожидание</option><option value="calibrate">Калибровка</option><option value="reserve">Ниже резерва</option><option value="critical">Ниже критического SOC</option><option value="brownout">Обесточивание</option><option value="lost">Работа в просроченных заданиях</option><option value="downlink">Сброс данных</option>{(filter.kind === 'satellite' || filter.kind === 'step' || filter.value) && <option value="custom">{filter.label}</option>}</select></label></div>}<TraceTable rows={reasonSearch?.rows ?? filteredRows} jobs={data.jobs} onSelect={openDecision} heading={reasonSearch ? `Ожидание · ${reasonNames[reasonSearch.code] ?? reasonSearch.code}` : filter.label} note={reasonSearch ? 'Только найденные совпадения среди уже проверенной части истории.' : 'Нажмите «Разбор» для выбора планировщика, альтернатив и последствий.'} /></div>
  </div>
}
