import { useEffect, useMemo, useRef, useState } from 'react'
import { useWorkspace } from '../workspace'
import { EmptyRun, ErrorNotice, Kpi, Loading, PageHeading, money, num, percent } from '../components/common'
import CostDrawer from '../components/CostDrawer'
import { FlowData, FlowJob, FlowStatus, loadFlow, statusNames, statuses } from './flowData'
import '../components/flow.css'

const ROW_HEIGHT = 62
type SortKey = 'job_id' | 'priority' | 'value_usd' | 'deadline_step' | 'progress'
export default function FlowPage() {
  const { run } = useWorkspace()
  const [snapshot, setSnapshot] = useState<(FlowData & { runId: string }) | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)
  const [status, setStatus] = useState<FlowStatus | ''>('')
  const [kind, setKind] = useState(''), [priority, setPriority] = useState(''), [satellite, setSatellite] = useState(''), [query, setQuery] = useState('')
  const [sort, setSort] = useState<SortKey>('deadline_step'), [descending, setDescending] = useState(false)
  const [scrollTop, setScrollTop] = useState(0), [height, setHeight] = useState(580)
  const [selection, setSelection] = useState<{ runId: string; job: FlowJob } | null>(null)
  const viewport = useRef<HTMLDivElement>(null)
  const runId = run?.run_id
  const data = snapshot?.runId === runId ? snapshot : null
  const selected = selection && selection.runId === runId ? selection.job : null
  const jobsTotal = run?.summary.jobs_total
  useEffect(() => { setSelection(null); setSnapshot(null); setQuery(''); setSatellite('') }, [runId])
  useEffect(() => {
    if (!runId) return
    let active = true
    setLoading(true); setError(null)
    void loadFlow(runId).then(value => { if (active) setSnapshot({ ...value, runId }) }).catch(failure => { if (active) setError(failure) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [runId, run?.step, jobsTotal, reload])
  useEffect(() => {
    if (!viewport.current) return
    const observer = new ResizeObserver(entries => setHeight(entries[0].contentRect.height))
    observer.observe(viewport.current)
    return () => observer.disconnect()
  }, [data])
  const rows = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru')
    const filtered = (data?.rows ?? []).filter(job => (!status || job.status === status) && (!kind || job.kind === kind) && (!priority || String(job.priority) === priority) && (!satellite || job.eligible_satellites.includes(satellite)) && (!needle || job.job_id.toLocaleLowerCase('ru').includes(needle)))
    const factor = descending ? -1 : 1
    return filtered.sort((a, b) => {
      const left = a[sort], right = b[sort]
      const order = typeof left === 'number' && typeof right === 'number' ? left - right : String(left).localeCompare(String(right), 'ru', { numeric: true })
      return factor * order || a.job_id.localeCompare(b.job_id)
    })
  }, [data, status, kind, priority, satellite, query, sort, descending])
  useEffect(() => { if (viewport.current) viewport.current.scrollTop = 0; setScrollTop(0) }, [status, kind, priority, satellite, query, sort, descending, runId])
  const start = Math.max(0, Math.min(rows.length - 1, Math.floor(scrollTop / ROW_HEIGHT) - 6))
  const visible = rows.slice(start, start + Math.ceil(height / ROW_HEIGHT) + 12)
  const setOrder = (key: SortKey) => { if (sort === key) setDescending(value => !value); else { setSort(key); setDescending(key === 'value_usd' || key === 'priority') } }
  const setSelected = (job: FlowJob | null) => setSelection(job && runId ? { runId, job } : null)
  if (!run) return <><PageHeading title="Поток заданий" subtitle="Прогресс, сроки и ресурсные затраты каждого задания." /><EmptyRun /></>
  return <>
    <PageHeading title="Поток заданий" subtitle={`Смена ${run.label || run.run_id} · данные на границе ${data?.step ?? run.step}`} actions={<button className="button" disabled={loading} onClick={() => setReload(value => value + 1)}>Обновить</button>} />
    <ErrorNotice error={error} />
    {loading && <Loading text={data ? 'Обновляем поток заданий…' : 'Загружаем полный поток, включая задания за пределами страницы API…'} />}
    {data && <>
      <div className="kpi-grid">{statuses.map(value => <Kpi key={value} label={statusNames[value]} value={num(data.counts[value])} onClick={() => setStatus(previous => previous === value ? '' : value)} />)}</div>
      {data.reconstructed && <p className="muted">Все {num(data.rows.length)} заданий восстановлены из полного экспорта: исходные данные, принятые сообщения и успешные действия журнала. API потока ограничивает размер корзин.</p>}
      {data.step !== run.step && <p className="flow-warning">Снимок потока — граница {data.step}, пульт — {run.step}. Обновите состояние перед сравнением показателей.</p>}
      <section className="panel">
        <div className="toolbar flow-filters"><label>Поиск<input className="field" value={query} onChange={e => setQuery(e.target.value)} placeholder="ID задания" /></label><label>Статус<select className="field" value={status} onChange={e => setStatus(e.target.value as FlowStatus | '')}><option value="">Все статусы</option>{statuses.map(value => <option key={value} value={value}>{statusNames[value]}</option>)}</select></label><label>Тип<select className="field" value={kind} onChange={e => setKind(e.target.value)}><option value="">Любой</option><option value="downlink">Downlink</option><option value="relay">Relay</option></select></label><label>Приоритет<select className="field" value={priority} onChange={e => setPriority(e.target.value)}><option value="">Любой</option>{[3, 2, 1].map(value => <option key={value} value={value}>P{value}</option>)}</select></label><label>Исполнитель<select className="field" value={satellite} onChange={e => setSatellite(e.target.value)}><option value="">Любой допустимый</option>{Object.keys(run.satellites).sort().map(id => <option key={id}>{id}</option>)}</select></label></div>
        <p className="muted">Найдено {num(rows.length)} из {num(data.rows.length)}. Нажмите заголовок для сортировки, задание — для карты затрат.</p>
        <div className="flow-virtual-shell" role="table" aria-label="Поток заданий" aria-rowcount={rows.length + 1} aria-colcount={8}>
          <div className="flow-grid flow-grid-head" role="row"><div role="columnheader" aria-sort={sort === 'job_id' ? descending ? 'descending' : 'ascending' : undefined}><button onClick={() => setOrder('job_id')}>Задание {sort === 'job_id' ? descending ? '↓' : '↑' : ''}</button></div><div role="columnheader">Статус / тип</div><div role="columnheader"><button onClick={() => setOrder('priority')}>Приоритет {sort === 'priority' ? descending ? '↓' : '↑' : ''}</button></div><div role="columnheader"><button onClick={() => setOrder('value_usd')}>Стоимость {sort === 'value_usd' ? descending ? '↓' : '↑' : ''}</button></div><div role="columnheader"><button onClick={() => setOrder('deadline_step')}>Окно {sort === 'deadline_step' ? descending ? '↓' : '↑' : ''}</button></div><div role="columnheader"><button onClick={() => setOrder('progress')}>Прогресс {sort === 'progress' ? descending ? '↓' : '↑' : ''}</button></div><div role="columnheader">Осталось</div><div role="columnheader">Допустимые аппараты</div></div>
          <div className="flow-viewport" ref={viewport} onScroll={e => setScrollTop(e.currentTarget.scrollTop)} role="rowgroup" tabIndex={0} aria-label="Прокручиваемый список заданий">
            <div style={{ height: rows.length * ROW_HEIGHT, position: 'relative', minWidth: 950 }}>
              {visible.map((job, i) => <div className="flow-grid flow-data-row" role="row" aria-rowindex={start + i + 2} key={job.job_id} style={{ position: 'absolute', top: (start + i) * ROW_HEIGHT, height: ROW_HEIGHT, width: '100%' }} onClick={() => setSelected(job)}><div role="cell"><button className="flow-job-link" onClick={e => { e.stopPropagation(); setSelected(job) }}>{job.job_id}</button></div><div role="cell"><span className={`badge flow-${job.status}`}>{statusNames[job.status]}</span><small>{job.kind}</small></div><div role="cell"><span className={`badge ${job.priority === 3 ? 'flow-critical' : ''}`}>P{job.priority}</span></div><div role="cell">{money(job.value_usd)}</div><div role="cell">{job.release_step} → {job.deadline_step}</div><div role="cell"><span>{percent(job.progress)}</span><progress max={1} value={job.progress} aria-label={`Прогресс ${job.job_id}`} /></div><div role="cell">{job.remaining_steps} / {job.work_steps}</div><div role="cell" title={job.eligible_satellites.join(', ')}>{job.eligible_satellites.join(', ')}</div></div>)}
            </div>
          </div>
          {!rows.length && <div className="empty">По выбранным фильтрам заданий нет.</div>}
        </div>
        <p className="muted">«Ожидают» включает ещё не начавшиеся окна. «В работе» — есть сохранённый прогресс, но не обязательно выполняется прямо сейчас. Частичное выполнение не приносит выручки.</p>
      </section>
    </>}
    {selected && <CostDrawer key={`${run.run_id}:${selected.job_id}`} scenarioId={run.scenario_id} jobId={selected.job_id} onClose={() => setSelected(null)} />}
  </>
}
