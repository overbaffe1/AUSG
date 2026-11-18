import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, Preflight } from '../api/client'
import { useWorkspace } from '../workspace'
import { ErrorNotice, Kpi, Loading, money, num, PageHeading, percent } from '../components/common'
import CostDrawer from '../components/CostDrawer'
interface InfeasibleJob { job_id:string;kind:string;priority:number;value_usd:number;work_steps:number;opportunities:number;reason:string }
export default function PreflightPage() {
  const { scenarios, scenarioId, setScenarioId, reloadScenarios, busy, task } = useWorkspace()
  const [report, setReport] = useState<Preflight|null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [costJob, setCostJob] = useState('')
  const [jobQuery, setJobQuery] = useState('')
  const [satId, setSatId] = useState('')
  const [soc, setSoc] = useState('')
  const [solar, setSolar] = useState('')
  const [priorityJob, setPriorityJob] = useState('')
  const [priority, setPriority] = useState('3')
  const [title, setTitle] = useState('')
  const [message, setMessage] = useState('')
  useEffect(() => {
    let active = true
    setReport(null); setError(null); setCostJob(''); setLoading(!!scenarioId)
    if (scenarioId) void api.preflight(scenarioId).then(r => { if(active) setReport(r) }).catch(e => { if(active) setError(e) }).finally(() => { if(active) setLoading(false) })
    return () => { active = false }
  }, [scenarioId])
  const selected = scenarios.find(s => s.id === scenarioId)
  const derive = () => task('Сохраняем новый сценарий…', async () => {
    const body: Record<string,unknown> = {}
    if (soc !== '') { if (!satId.trim()) throw new Error('Укажите ID аппарата для изменения заряда.'); body.initial_soc_pct = { [satId.trim()]: Number(soc) } }
    if (solar !== '') body.solar_factor = Number(solar)
    if (priorityJob.trim()) body.job_priority = { [priorityJob.trim()]: Number(priority) }
    if (!Object.keys(body).length) throw new Error('Задайте хотя бы одно изменение сценария.')
    if (title.trim()) body.title = title.trim()
    const value = await api.derive(scenarioId, body)
    await reloadScenarios(); setScenarioId(value.id); setMessage(`Сохранён новый сценарий «${value.title}». Выберите его на пульте смены.`)
  })
  return <>
    <PageHeading title="Предполётная оценка" subtitle="Оцените спрос, связь и запас ресурсов до начала смены." actions={<Link className="button" to="/shift">К пульту смены →</Link>} />
    <section className="panel toolbar"><label className="field wide">Сценарий<select value={scenarioId} disabled={busy} onChange={e=>{setScenarioId(e.target.value);setMessage('')}}>{scenarios.map(s=><option key={s.id} value={s.id}>{s.id} · {s.title}</option>)}</select></label><span className="badge">{num(selected?.satellites)} аппаратов</span><span className="badge">{num(selected?.steps)} шагов</span><span className="badge">{num(selected?.jobs_total)} заданий</span></section>
    <ErrorNotice error={error} />{message && <div className="notice success" role="status">{message}</div>}{loading && <Loading text="Оцениваем доступные ресурсы…" />}
    {report && <>
      <div className="kpi-grid"><Kpi label="Общая стоимость заданий" value={money(report.money.total_usd)} note="Весь входящий поток" /><Kpi label="Потолок выручки" value={money(report.money.reachable_ceiling_usd)} note="За вычетом структурно невыполнимых" /><Kpi label="Энергодефицит" value={<span className="warn">{report.energy_short_satellites.length} / {report.satellites}</span>} note="Аппаратов с дефицитом по оценке API" /><Kpi label="Структурно невыполнимо" value={<span className="bad">{num(report.structural_infeasible.count)}</span>} note={money(report.structural_infeasible.value_usd)} /></div>
      <div className="two-column"><section className="panel"><div className="panel-heading"><h3><span className="section-number">01</span>Спрос и доступная ёмкость</h3><span className="badge">С учётом окон связи</span></div>
        {Object.entries(report.capacity).map(([key,v])=><div key={key}><div className="capacity-label"><strong>{key==='downlink'?'Передача на Землю':'Ретрансляция'} <span className="muted">/ {key}</span></strong><strong className={(v.oversubscription??0)>1?'warn':''}>{percent(v.oversubscription)}</strong></div><div className={'meter '+((v.oversubscription??0)>1?'warning':'')}><span style={{width:`${Math.min(100,(v.oversubscription??0)*100)}%`}} /></div><p className="muted" style={{fontSize:11,marginTop:8}}>Спрос {num(v.demand_steps)} / ёмкость {num(v.capacity_steps)} шаго-слотов{(v.oversubscription??0)>1?' · Спрос превышает ёмкость':''}</p></div>)}
        <p className="muted" style={{fontSize:11}}>Потолок выручки — оценка после исключения заведомо невыполнимых заданий. Конкуренция за ресурсы может дополнительно снизить результат.</p></section>
        <section className="panel"><h3><span className="section-number">02</span>Доступность связи</h3><table><tbody><tr><td>Единственный аппарат в downlink-окне</td><td>{report.ground_link.steps_with_single_gateway} шагов</td></tr><tr><td>Максимальный разрыв downlink</td><td>{report.ground_link.longest_gap_steps} шагов</td></tr><tr><td>Нет доступных сеансов обоих типов</td><td className={report.ground_link.blind_steps.length?'warn':'ok'}>{report.ground_link.blind_steps.length} шагов</td></tr></tbody></table><details><summary>Шаги без доступных сеансов</summary><p className="mono">{report.ground_link.blind_steps.join(', ') || 'Нет'}</p></details><p className="muted" style={{fontSize:11}}>При отсутствии обоих каналов задания не выполняются по модели. Ожидание и калибровка учитываются отдельно.</p></section></div>
      <section className="panel"><div className="panel-heading"><h3><span className="section-number">03</span>Структурно невыполнимые задания</h3><span className="badge">{num(report.structural_infeasible.count)} заданий</span></div><p className="muted" style={{fontSize:12}}>Нажмите ID задания, чтобы разобрать ресурсные затраты.</p><div className="table-wrap table-scroll"><table><thead><tr><th>Задание</th><th>Тип</th><th>Приоритет</th><th>Стоимость</th><th>Нужно шагов</th><th>Доступно возможностей</th></tr></thead><tbody>{(report.structural_infeasible.jobs as InfeasibleJob[]).map(j=><tr key={j.job_id}><td><button className="link mono" onClick={()=>setCostJob(j.job_id)}>{j.job_id} ↗</button></td><td>{j.kind}</td><td>P{j.priority}</td><td>{money(j.value_usd)}</td><td>{j.work_steps}</td><td className="bad">{j.opportunities}</td></tr>)}{!report.structural_infeasible.count && <tr><td colSpan={6}>Структурно невыполнимых заданий нет.</td></tr>}</tbody></table></div>{report.structural_infeasible.truncated && <p className="muted">Сервер передаёт первые 200 заданий из {num(report.structural_infeasible.count)}.</p>}
        <form className="toolbar" style={{marginTop:16}} onSubmit={e=>{e.preventDefault();if(jobQuery.trim())setCostJob(jobQuery.trim())}}><label className="field">Карта любого задания по ID<input value={jobQuery} onChange={e=>setJobQuery(e.target.value)} placeholder="ID из исходного сценария" required /></label><button className="ghost" type="submit">Открыть карту</button></form></section>
      <details className="panel"><summary>Подробности дефицита ресурсов · {report.energy_short_satellites.length} аппаратов</summary><div className="two-column"><div><h4>Оценка энергобюджета</h4><div className="table-wrap table-scroll"><table><thead><tr><th>Аппарат</th><th>Бюджет, Вт·ч</th><th>Спрос, Вт·ч</th></tr></thead><tbody>{report.energy_short_satellites.map(s=><tr key={s.satellite_id}><td>{s.satellite_id}</td><td>{num(s.budget_wh,1)}</td><td className="warn">{num(s.demand_wh,1)}</td></tr>)}</tbody></table></div></div><div><h4>Спрос на downlink выше собственного окна</h4><div className="table-wrap table-scroll"><table><thead><tr><th>Аппарат</th><th>Спрос</th><th>Окно, шагов</th></tr></thead><tbody>{report.starved_satellites.map(s=><tr key={s.satellite_id}><td>{s.satellite_id}</td><td>{s.demand_steps}</td><td>{s.contact_steps}</td></tr>)}</tbody></table></div></div></div></details>
    </>}
    <details className="panel"><summary>Изменить исходный сценарий</summary><p className="muted">Изменения сохранятся новым сценарием. Укажите существующие ID из исходного JSON; полный каталог до запуска сервер не передаёт.</p><form onSubmit={e=>{e.preventDefault();void derive()}}><fieldset disabled={busy} style={{border:0,padding:0,margin:0}}><div className="form-grid"><label className="field">Название варианта<input value={title} onChange={e=>setTitle(e.target.value)} placeholder="Мой сценарий" /></label><label className="field">Коэффициент солнечной мощности<input type="number" min="0" step="0.01" value={solar} onChange={e=>setSolar(e.target.value)} placeholder="Без изменения" /></label><label className="field">ID аппарата<input value={satId} onChange={e=>setSatId(e.target.value)} placeholder="ID из сценария" /></label><label className="field">Начальный заряд, %<input type="number" min="0" max="100" step="0.1" value={soc} onChange={e=>setSoc(e.target.value)} placeholder="Без изменения" /></label><label className="field">ID задания для смены приоритета<input value={priorityJob} onChange={e=>setPriorityJob(e.target.value)} placeholder="ID из сценария" /></label><label className="field">Новый приоритет<select value={priority} onChange={e=>setPriority(e.target.value)}><option value="1">P1</option><option value="2">P2</option><option value="3">P3</option></select></label></div><button style={{marginTop:18}} disabled={!scenarioId}>Сохранить как новый сценарий</button></fieldset></form></details>
    <details className="panel"><summary>Загрузить свой сценарий</summary><label className="field">Файл сценария JSON<input type="file" accept=".json,application/json" disabled={busy} onChange={e=>{const file=e.target.files?.[0];e.target.value='';if(file)void task('Проверяем сценарий…',async()=>{const data=JSON.parse((await file.text()).replace(/^\uFEFF/,''));const value=await api.uploadScenario(data);await reloadScenarios();setScenarioId(value.id);setMessage(`Сценарий «${value.title}» загружен и проверен сервером.`)})}} /></label></details>
    {costJob && <CostDrawer scenarioId={scenarioId} jobId={costJob} onClose={()=>setCostJob('')} />}
  </>
}
