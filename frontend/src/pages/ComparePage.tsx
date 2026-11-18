import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, Mode, Objective, RunState } from '../api/client'
import { EmptyRun, ErrorNotice, Loading, PageHeading, money, num } from '../components/common'
import { useWorkspace } from '../workspace'
import { FlowJob, loadFlow, statusNames } from './flowData'
import { CHECKPOINT_PREFIX, ComparisonResponse, RunEntry, checkpointEntries, equalData, modeLabel, objectiveLabel, recordOf, saveCheckpoint, verifyBranches } from './compareExportData'
import './compare-export.css'

interface Variant { label: string; objective: Objective; mode: Mode }
interface Result { response: ComparisonResponse; source: RunState; a: RunState; b: RunState; jobsA: FlowJob[]; jobsB: FlowJob[]; target: number }
const metricRows = [
  ['revenue_usd', 'Выручка, USD', 2], ['critical_jobs_completed_on_time', 'P3 в срок', 0], ['critical_jobs_due', 'P3 с наступившим сроком', 0],
  ['jobs_completed', 'Завершено заданий', 0], ['jobs_due_missed', 'Просрочено заданий', 0], ['work_steps_in_missed_jobs', 'Работа в просроченных, шаги', 0], ['minimum_soc_pct', 'Минимальный SOC, %', 2],
] as const
const delta = (value: unknown, digits = 0) => typeof value === 'number' && Number.isFinite(value) ? `${value > 0 ? '+' : ''}${num(value, digits)}` : '—'

function PairChart({ title, a, b, monetary = false }: { title: string; a: unknown; b: unknown; monetary?: boolean }) {
  const values = [a, b].map(v => typeof v === 'number' && Number.isFinite(v) ? v : null)
  const max = Math.max(0, ...values.filter((v): v is number => v !== null)) || 1
  return <section className="cx-pair-chart" aria-label={title}><h4>{title}</h4>{values.map((value, index) => <div className="cx-bar-row" key={index}><span>{index ? 'B' : 'A'}</span><div className="cx-bar-track"><i className={index ? 'branch-b' : 'branch-a'} style={{ width: `${value === null ? 0 : Math.max(0, value) / max * 100}%` }} /></div><strong>{monetary ? money(value) : num(value)}</strong></div>)}<small className="muted">Начало шкалы — 0. Значения за весь выполненный период.</small></section>
}

export default function ComparePage() {
  const { run, busy, task, selectRun } = useWorkspace()
  const navigate = useNavigate()
  const [checkpoints, setCheckpoints] = useState<RunEntry[]>([])
  const [source, setSource] = useState<RunState | null>(null)
  const [selected, setSelected] = useState('current')
  const [label, setLabel] = useState('')
  const [target, setTarget] = useState(0)
  const [variantA, setVariantA] = useState<Variant>({ label: 'A · Приоритеты', objective: 'priority_service', mode: 'centralized' })
  const [variantB, setVariantB] = useState<Variant>({ label: 'B · Выручка', objective: 'commercial', mode: 'centralized' })
  const [preset, setPreset] = useState('objectives')
  const [result, setResult] = useState<Result | null>(null)
  const [loading, setLoading] = useState(false)
  const [localError, setLocalError] = useState<unknown>(null)
  const [message, setMessage] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)
  const epoch = useRef(0)

  useEffect(() => {
    const current = ++epoch.current
    setSource(run); setSelected('current'); setTarget(run?.steps_total ?? 0); setLabel(run ? `${run.scenario_id}, шаг ${run.step}` : '')
    setResult(null); setMessage(''); setLocalError(null); setQuery(''); setPage(0)
    if (run) { setVariantA({ label: 'A · Приоритеты', objective: 'priority_service', mode: run.mode }); setVariantB({ label: 'B · Выручка', objective: 'commercial', mode: run.mode }); setPreset('objectives') }
    setLoading(true)
    void api.runs().then(value => { if (current === epoch.current) setCheckpoints(checkpointEntries(value.runs)) }).catch(error => { if (current === epoch.current) setLocalError(error) }).finally(() => { if (current === epoch.current) setLoading(false) })
    return () => { epoch.current++ }
  }, [run?.run_id, run?.state_digest])

  const reloadCheckpoints = async () => { setCheckpoints(checkpointEntries((await api.runs()).runs)) }
  const chooseSource = (id: string) => {
    if (!run) return
    void task('Открываем контрольную точку…', async () => {
      const value = id === 'current' ? await api.run(run.run_id) : await api.run(id)
      setSource(value); setSelected(id); setTarget(value.steps_total); setResult(null); setMessage(''); setQuery(''); setPage(0)
      setVariantA({ label: 'A · Приоритеты', objective: 'priority_service', mode: value.mode }); setVariantB({ label: 'B · Выручка', objective: 'commercial', mode: value.mode }); setPreset('objectives')
    })
  }
  const preserve = () => {
    if (!run) return
    void task('Сохраняем контрольную точку…', async () => {
      const saved = await saveCheckpoint(run, label)
      await reloadCheckpoints(); setSource(saved); setSelected(saved.run_id); setTarget(saved.steps_total); setResult(null)
      setMessage(`Сохранена точка на границе ${saved.step}. Исходный расчёт можно продолжать на пульте; эта копия остаётся отдельно.`)
    })
  }
  const usePreset = (name: string) => {
    if (!source) return
    setPreset(name); setResult(null)
    if (name === 'autonomy') { setVariantA({ label: 'A · Централизованный', objective: source.objective, mode: 'centralized' }); setVariantB({ label: 'B · Автономный', objective: source.objective, mode: 'federated' }) }
    else { setVariantA({ label: 'A · Приоритеты', objective: 'priority_service', mode: source.mode }); setVariantB({ label: 'B · Выручка', objective: 'commercial', mode: source.mode }) }
  }
  const compare = () => {
    if (!source) return
    void task('Создаём и рассчитываем два продолжения…', async () => {
      setResult(null); setMessage(''); setPage(0); setQuery('')
      if (!Number.isInteger(target) || target <= source.step || target > source.steps_total) throw new Error(`Конечная граница должна быть целым числом от ${source.step + 1} до ${source.steps_total}.`)
      // Отдельная копия текущего состояния не зависит от дальнейшей работы на пульте.
      const anchor = selected === 'current' ? await saveCheckpoint(source, `${source.scenario_id}, шаг ${source.step}`) : await api.run(source.run_id)
      if (anchor.step !== source.step || anchor.state_digest !== source.state_digest) throw new Error('Выбранная точка изменилась. Выберите её заново и проверьте границу.')
      setSource(anchor); setSelected(anchor.run_id); await reloadCheckpoints()
      const initial = recordOf(await api.exportRun(anchor.run_id, false))
      if (initial.steps_executed !== anchor.step) throw new Error('Граница контрольной точки изменилась во время запроса.')
      const planner = anchor.planner === 'baseline' ? 'baseline' : 'economic'
      const branchLabel = (side: string, value: string) => `${side} · ${value.trim().replace(/^[AB]\s*[·:]\s*/, '') || 'Продолжение'}`
      const a = await api.branch(anchor.run_id, { ...variantA, label: branchLabel('A', variantA.label), planner, config: initial.run_metadata.parameters })
      const b = await api.branch(anchor.run_id, { ...variantB, label: branchLabel('B', variantB.label), planner, config: initial.run_metadata.parameters })
      if (a.step !== anchor.step || b.step !== anchor.step || a.state_digest !== anchor.state_digest || b.state_digest !== anchor.state_digest) throw new Error('Ветви получили разные начальные состояния. Сравнение остановлено.')
      // Последовательный расчёт сохраняет отзывчивость сервера на больших сценариях.
      const endA = (await api.advance(a.run_id, { to_step: target })).state
      const endB = (await api.advance(b.run_id, { to_step: target })).state
      const [rawA, rawB, rawComparison, jobsA, jobsB] = await Promise.all([api.exportRun(a.run_id, false), api.exportRun(b.run_id, false), api.compare(a.run_id, b.run_id), loadFlow(a.run_id), loadFlow(b.run_id)])
      const ea = recordOf(rawA), eb = recordOf(rawB)
      verifyBranches(anchor, initial, endA, endB, ea, eb, target)
      const response = rawComparison as ComparisonResponse
      if (!response?.comparable_state || response.a?.run_id !== a.run_id || response.b?.run_id !== b.run_id || !response.verdict?.text || jobsA.step !== target || jobsB.step !== target || !equalData(response.a.summary, ea.summary) || !equalData(response.b.summary, eb.summary) || !equalData(endA.summary, ea.summary) || !equalData(endB.summary, eb.summary)) throw new Error('Сервер вернул несогласованные результаты сравнения. Возможно, одна из ветвей изменилась во время запроса.')
      setResult({ response, source: anchor, a: endA, b: endB, jobsA: jobsA.rows, jobsB: jobsB.rows, target })
    })
  }
  const differences = useMemo(() => {
    if (!result) return []
    const rowsA = new Map(result.jobsA.map(job => [job.job_id, job])), rowsB = new Map(result.jobsB.map(job => [job.job_id, job]))
    return [...new Set([...rowsA.keys(), ...rowsB.keys()])].map(id => ({ id, a: rowsA.get(id), b: rowsB.get(id) })).filter(({ a, b }) => a?.status !== b?.status || a?.remaining_steps !== b?.remaining_steps || a?.completed_step !== b?.completed_step).sort((a, b) => Math.max(b.a?.priority ?? 0, b.b?.priority ?? 0) - Math.max(a.a?.priority ?? 0, a.b?.priority ?? 0) || a.id.localeCompare(b.id))
  }, [result])
  const filtered = differences.filter(row => row.id.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
  const renderVariant = (name: 'A' | 'B', value: Variant, setter: (value: Variant) => void) => <fieldset className={`cx-variant branch-${name.toLowerCase()}`} disabled={busy}><legend>Вариант {name}</legend><label>Название<input value={value.label} maxLength={90} onChange={e => { setter({ ...value, label: e.target.value }); setPreset('custom'); setResult(null) }} /></label><label>Цель<select value={value.objective} onChange={e => { setter({ ...value, objective: e.target.value as Objective }); setPreset('custom'); setResult(null) }}><option value="priority_service">Приоритетное обслуживание</option><option value="commercial">Коммерческая отдача</option></select></label><label>Исполнение<select value={value.mode} onChange={e => { setter({ ...value, mode: e.target.value as Mode }); setPreset('custom'); setResult(null) }}><option value="centralized">Централизованное управление</option><option value="federated">Бортовая автономия</option></select></label></fieldset>
  return <div className="cx-page"><PageHeading title="Сравнение вариантов" subtitle="Два продолжения из одного состояния. Одинаковая история и конечная граница." /><ErrorNotice error={localError} />{!run ? <EmptyRun /> : <>
    <section className="card"><div className="section-heading"><div><h3>Контрольная точка</h3><p className="muted">Можно сохранить только текущую границу: {run.step} из {run.steps_total}. Вернуться к прошлому можно через ранее сохранённую копию.</p></div></div><div className="cx-form-row"><label>Название новой точки<input value={label} onChange={e => setLabel(e.target.value)} maxLength={100} disabled={busy} /></label><button className="ghost" onClick={preserve} disabled={busy}>Сохранить текущую точку</button></div><div className="cx-form-row"><label>Источник сравнения<select value={selected} onChange={e => chooseSource(e.target.value)} disabled={busy || loading}><option value="current">Текущий расчёт · {run.scenario_id} · шаг {run.step}</option>{checkpoints.map(point => <option key={point.run_id} value={point.run_id}>{point.label.slice(CHECKPOINT_PREFIX.length)} · {point.scenario_id} · шаг {point.step}</option>)}</select></label><button className="ghost" disabled={busy || loading} onClick={() => void task('Обновляем контрольные точки…', reloadCheckpoints)}>Обновить список</button></div>{loading && <Loading text="Загружаем сохранённые точки…" />}{message && <p className="notice success" role="status">{message}</p>}{source && <p className="muted">Источник: {source.label || source.run_id} · {source.scenario_id} · граница {source.step}. Точки хранятся на сервере до его перезапуска.</p>}</section>
    {source && <section className="card"><div className="section-heading"><h3>Настройки продолжений</h3><div className="actions"><button className={preset === 'objectives' ? '' : 'ghost'} onClick={() => usePreset('objectives')} disabled={busy}>Две цели</button><button className={preset === 'autonomy' ? '' : 'ghost'} onClick={() => usePreset('autonomy')} disabled={busy}>Цена автономии</button></div></div><div className="cx-two-columns">{renderVariant('A', variantA, setVariantA)}{renderVariant('B', variantB, setVariantB)}</div><p className="muted">Алгоритм источника: {source.planner}. Параметры сохраняются. В автономном режиме решения принимает бортовой планировщик. Новые сообщения после развилки в этот прогноз не добавляются.</p><div className="cx-form-row"><label>Общая конечная граница<input type="number" min={source.step + 1} max={source.steps_total} value={target} onChange={e => { setTarget(Number(e.target.value)); setResult(null) }} disabled={busy || source.finished} /></label><button onClick={compare} disabled={busy || source.finished || !Number.isInteger(target) || target <= source.step || target > source.steps_total}>Рассчитать два варианта →</button></div>{source.finished && <p className="notice">Эта точка завершена. Выберите сохранённую точку до конца смены.</p>}</section>}
    {result && <><section className="card cx-verdict"><span className="eyebrow">Вердикт сервера</span><h3>{result.response.verdict.text}</h3><p className="muted">Оценка по цели A: {objectiveLabel(result.a.objective)}. Сервер считает результаты сопоставимыми в пределах 1% основного показателя. Точные значения и разницы приведены ниже.</p><div className="cx-proof"><strong>Общее состояние подтверждено</strong><span>Граница {result.source.step} → {result.target} · сценарий {result.source.scenario_id}</span><code>{result.source.state_digest}</code><span>Совпадают исходный сценарий, выполненные команды до развилки и полученные сообщения.</span></div></section><div className="cx-two-columns">{[result.a, result.b].map((branch, index) => <section className={`card cx-result-card branch-${index ? 'b' : 'a'}`} key={branch.run_id}><span className="eyebrow">Вариант {index ? 'B' : 'A'}</span><h3>{branch.label}</h3><p>{objectiveLabel(branch.objective)}<br /><span className="muted">{modeLabel(branch.mode)}</span></p><strong className="cx-big-number">{money(branch.summary.revenue_usd)}</strong><p>P3 в срок: {num(branch.summary.critical_jobs_completed_on_time)} / {num(branch.summary.critical_jobs_due)}</p><button className="ghost" disabled={busy} onClick={() => void task('Открываем выбранное продолжение…', async () => { selectRun(await api.run(branch.run_id)); navigate('/shift') })}>{branch.finished ? 'Открыть результат' : 'Продолжить эту ветвь'} →</button></section>)}</div><section className="card"><h3>Итоги и разница B − A</h3><p className="muted">Метрики включают общую историю до контрольной точки. Знак показывает направление разницы, а не оценку качества.</p><div className="table-scroll"><table><thead><tr><th>Показатель</th><th>A</th><th>B</th><th>B − A</th></tr></thead><tbody>{metricRows.map(([key, title, digits]) => <tr key={key}><th>{title}</th><td>{num(result.response.a.summary[key], digits)}</td><td>{num(result.response.b.summary[key], digits)}</td><td className="cx-delta">{delta(result.response.deltas[key], digits)}</td></tr>)}</tbody></table></div><div className="cx-two-columns"><PairChart title="Выручка, USD" monetary a={result.a.summary.revenue_usd} b={result.b.summary.revenue_usd} /><PairChart title="P3 завершено в срок" a={result.a.summary.critical_jobs_completed_on_time} b={result.b.summary.critical_jobs_completed_on_time} /></div>{result.a.mode === 'centralized' && result.b.mode === 'federated' && result.a.objective === result.b.objective && <p className="notice">Разница выручки автономного и централизованного вариантов: <strong>{delta(result.response.deltas.revenue_usd, 2)} USD</strong>. Отрицательное значение означает меньшую выручку автономного варианта в этом сравнении.</p>}</section><section className="card"><div className="section-heading"><div><h3>Различия заданий <span className="muted">{num(differences.length)}</span></h3><p className="muted">Разный статус, прогресс или момент завершения. P3 показаны первыми.</p></div><label className="cx-search">Поиск ID<input value={query} onChange={e => { setQuery(e.target.value); setPage(0) }} placeholder="Например, J-001" /></label></div>{filtered.length ? <><div className="table-scroll"><table><thead><tr><th>Задание</th><th>Приоритет</th><th>A · статус и прогресс</th><th>B · статус и прогресс</th><th>Завершение A / B</th></tr></thead><tbody>{filtered.slice(page * 30, page * 30 + 30).map(row => <tr key={row.id} className={row.a?.status !== row.b?.status ? 'cx-status-diff' : ''}><td><code>{row.id}</code></td><td>P{row.a?.priority ?? row.b?.priority}</td>{[row.a, row.b].map((job, i) => <td key={i}>{job ? <><span className={`cx-job-status ${job.status}`}>{statusNames[job.status]}</span><small>{num(job.work_steps - job.remaining_steps)} / {num(job.work_steps)} шагов</small></> : 'Отсутствует'}</td>)}<td>{num(row.a?.completed_step)} / {num(row.b?.completed_step)}</td></tr>)}</tbody></table></div><div className="cx-pagination"><span>{page * 30 + 1}–{Math.min(page * 30 + 30, filtered.length)} из {num(filtered.length)}</span><button className="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>←</button><button className="ghost" disabled={(page + 1) * 30 >= filtered.length} onClick={() => setPage(page + 1)}>→</button></div></> : <div className="cx-empty">{query ? 'По этому ID различий нет.' : 'Статусы, прогресс и моменты завершения всех заданий совпали. Разные настройки могут дать одинаковый результат.'}</div>}</section></>}
  </>}</div>
}
