import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, Mode, Objective, RunState } from '../api/client'
import { useWorkspace } from '../workspace'
import { Kpi, money, num, PageHeading } from '../components/common'
interface EventMessage { id:string;at_step:number;type:string;[key:string]:unknown }
interface History { events: EventMessage[]; run_metadata?: { objective_changes?: {step:number;from:string;to:string}[];parameters?:Record<string,unknown> } }
const objectiveName = (value:string) => value==='commercial'?'Коммерческая отдача':'Приоритетное обслуживание'
const eventName = (value:string) => ({satellite_outage:'Недоступность аппаратов',close_downlink:'Закрытие downlink',add_jobs:'Новые задания'}[value]??value)
function readQueue(id:string): EventMessage[] { try { return JSON.parse(sessionStorage.getItem('kosmos-events-'+id)??'[]') as EventMessage[] } catch { return [] } }
function saveQueue(id:string,events:EventMessage[]) { try { sessionStorage.setItem('kosmos-events-'+id,JSON.stringify(events)) } catch { /* Текущий сеанс продолжает работать в памяти. */ } }
export default function ShiftPage() {
  const {run,selectRun,scenarios,scenarioId,setScenarioId,busy,task,openDecision} = useWorkspace()
  const [objective,setObjective]=useState<Objective>('priority_service')
  const [mode,setMode]=useState<Mode>('centralized')
  const [planner,setPlanner]=useState('economic')
  const [label,setLabel]=useState('')
  const [target,setTarget]=useState(72)
  const [running,setRunning]=useState(false)
  const stop = useRef(false)
  const [text,setText]=useState('')
  const [notice,setNotice]=useState('')
  const [history,setHistory]=useState<History|null>(null)
  const [queue,setQueue]=useState<EventMessage[]>([])
  const queued = useRef<EventMessage[]>([])
  const [eventType,setEventType]=useState<'satellite_outage'|'close_downlink'>('satellite_outage')
  const [sats,setSats]=useState<string[]>([])
  const [end,setEnd]=useState(90)
  useEffect(()=>()=>{stop.current=true},[])
  useEffect(()=>{
    stop.current=true;setText('');setNotice('');setHistory(null);setSats([])
    queued.current=run?readQueue(run.run_id):[];setQueue(queued.current)
    if(run){setObjective(run.objective);setEnd(Math.min(run.steps_total,Math.max(run.step+1,90)));setTarget(Math.min(run.steps_total,Math.max(run.step,72)))}
  },[run?.run_id])
  useEffect(()=>{
    if(!run)return
    let active=true
    setObjective(run.objective)
    void api.exportRun(run.run_id,false).then(data=>{if(active)setHistory(data as History)}).catch(()=>{if(active)setHistory(null)})
    return()=>{active=false}
  },[run])
  const updateQueue=(id:string,events:EventMessage[])=>{queued.current=events;setQueue(events);saveQueue(id,events)}
  const loadHistory=async(id:string)=>setHistory(await api.exportRun(id,false) as History)
  const safely = async (current:RunState, action:()=>Promise<void>) => {
    setNotice('')
    let beforeHistory: History | null = null
    try { beforeHistory = await api.exportRun(current.run_id,false) as History; await action();await loadHistory(current.run_id) }
    catch(error){
      try {
        const fresh=await api.run(current.run_id);selectRun(fresh)
        const afterHistory=await api.exportRun(current.run_id,false) as History;setHistory(afterHistory)
        const acceptedIds=new Set(afterHistory.events.map(e=>e.id));updateQueue(current.run_id,queued.current.filter(e=>!acceptedIds.has(e.id)))
        const unchanged=beforeHistory!==null&&fresh.state_digest===current.state_digest&&JSON.stringify(beforeHistory.events)===JSON.stringify(afterHistory.events)
        setNotice(unchanged?'Сообщение отклонено. Сервер подтвердил: состояние и лента сообщений сохранены без изменений.':'Обработка остановлена. Состояние перечитано с сервера; ранее принятые сообщения остаются в ленте.')
      } catch { setNotice('Ответ не получен. Перед повторной подачей обновите расчёт и проверьте ленту принятых сообщений.') }
      throw error
    }
  }
  const sendOne = async(current:RunState,event:EventMessage) => {
    const result=await api.sendEvent(current.run_id,event) as {state:RunState;accepted:unknown[];not_for_this_step:{id:string;at_step:number}[]}
    if(result.not_for_this_step.length)throw new Error(`Сообщение ${event.id} предназначено для шага ${event.at_step}. Текущий шаг: ${result.state.step}. Состояние не изменено.`)
    selectRun(result.state);return result.state
  }
  const submitJSON=()=>task('Принимаем сообщения…',async()=>{
    if(!run)return
    await safely(run,async()=>{
      let payload:unknown
      try{payload=JSON.parse(text.replace(/^\uFEFF/,''))}catch{throw new Error('Не удалось прочитать JSON. Проверьте запятые, кавычки и структуру сообщения.')}
      if(!payload||typeof payload!=='object'||Array.isArray(payload))throw new Error('Ожидается объект события или файл cosmo-B-events-1.0.')
      const object=payload as Record<string,unknown>
      const envelope=object.schema_version==='cosmo-B-events-1.0'
      const events=(envelope?object.events:[object]) as EventMessage[]
      if(!Array.isArray(events)||!events.length)throw new Error('Список событий пуст или имеет неверную структуру.')
      for(const event of events){
        if(!event||typeof event!=='object'||typeof event.id!=='string'||!event.id||!Number.isInteger(event.at_step)||typeof event.type!=='string')throw new Error('У каждого события нужны id, целочисленный at_step и type.')
        if(event.at_step<run.step||event.at_step>=run.steps_total)throw new Error(`Шаг сообщения ${event.id}: ${event.at_step}. Допустимо от ${run.step} до ${run.steps_total-1}.`)
        if(!envelope&&event.at_step!==run.step)throw new Error(`Сообщение предназначено для шага ${event.at_step}, а текущий шаг ${run.step}. Перейдите к нужной границе шага.`)
      }
      const ids=events.map(e=>e.id)
      if(new Set(ids).size!==ids.length||events.some(e=>queued.current.some(q=>q.id===e.id)))throw new Error('Повторный id в файле или очереди сообщений. Используйте уникальные идентификаторы.')
      let current=run;let accepted=0
      for(const event of events.filter(e=>e.at_step===run.step)){current=await sendOne(current,event);accepted++}
      const future=events.filter(e=>e.at_step>run.step)
      updateQueue(run.run_id,[...queued.current,...future].sort((a,b)=>a.at_step-b.at_step))
      setNotice(`Принято на текущем шаге: ${accepted}. В очереди будущих шагов: ${queued.current.length}. События очереди будут поданы по порядку при продолжении на этом пульте.`)
    })
  })
  const advance=(to:number)=>task(`Выполняем расчёт до шага ${to}…`,async()=>{
    if(!run)return
    if(!Number.isInteger(to)||to<=run.step||to>run.steps_total)throw new Error(`Выберите целый шаг больше ${run.step} и не больше ${run.steps_total}.`)
    stop.current=false;setRunning(true);setNotice('')
    let current=run
    try{
      while(current.step<to&&!stop.current){
        if(queued.current.some(e=>e.at_step<current.step))throw new Error('В очереди есть пропущенные события. Удалите их или откройте сохранённую контрольную точку.')
        for(const event of queued.current.filter(e=>e.at_step===current.step)){
          if(stop.current)break
          current=await sendOne(current,event)
          updateQueue(current.run_id,queued.current.filter(e=>e.id!==event.id))
        }
        if(stop.current)break
        const nextEvent=queued.current.find(e=>e.at_step>current.step)?.at_step??to
        const next=Math.min(current.step+12,to,nextEvent)
        current=(await api.advance(current.run_id,{to_step:next})).state;selectRun(current)
      }
      setNotice(`Расчёт остановлен на границе шага ${current.step}. ${queued.current.some(e=>e.at_step===current.step)?'События этой границы будут поданы перед следующим шагом.':''}`)
    }catch(error){try{const fresh=await api.run(run.run_id);selectRun(fresh);const history=await api.exportRun(run.run_id,false) as History;setHistory(history);const ids=new Set(history.events.map(e=>e.id));updateQueue(run.run_id,queued.current.filter(e=>!ids.has(e.id)));setNotice(`Расчёт остановлен на шаге ${fresh.step}; состояние обновлено с сервера.`)}catch{/* Основная ошибка уже выводится. */}throw error}
    finally{setRunning(false)}
  })
  const checkpoint=()=>task('Сохраняем контрольную точку…',async()=>{
    if(!run)return
    const data=await api.exportRun(run.run_id,false) as History
    const branch=await api.branch(run.run_id,{label:`Контрольная точка · ${run.scenario_id} · шаг ${run.step}`,objective:run.objective,mode:run.mode,planner:run.planner,config:data.run_metadata?.parameters})
    setNotice(`Контрольная точка шага ${branch.step} сохранена (${branch.run_id}). Откройте «Сравнение», чтобы создать два независимых продолжения.`)
  })
  let fileScenario: string | null = null
  try { const parsed = JSON.parse(text.replace(/^\uFEFF/,'')); if(parsed?.schema_version === 'cosmo-B-events-1.0' && typeof parsed.base_scenario === 'string') fileScenario=parsed.base_scenario } catch { /* Ошибка структуры будет показана при подаче. */ }
  const s=run?.summary??{}
  return <>
    <PageHeading title="Пульт смены" subtitle="Задайте цель. Планировщик самостоятельно выбирает задания и исполнителей." actions={run&&<Link to="/flow" className="button ghost">Поток заданий →</Link>} />
    <section className="panel"><div className="panel-heading"><h3>Новый расчёт</h3><span className="badge">{run?'Текущий расчёт сохранится':'Подготовка смены'}</span></div><form className="toolbar" onSubmit={e=>{e.preventDefault();void task('Создаём расчёт…',async()=>{const value=await api.createRun({scenario_id:scenarioId,objective,mode,planner:mode==='federated'?'economic':planner,label:label.trim()});selectRun(value)})}}><label className="field wide">Сценарий<select disabled={busy} value={scenarioId} onChange={e=>setScenarioId(e.target.value)}>{scenarios.map(v=><option key={v.id} value={v.id}>{v.id} · {v.jobs_total} заданий</option>)}</select></label><label className="field">Цель нового расчёта<select value={objective} disabled={busy} onChange={e=>setObjective(e.target.value as Objective)}><option value="priority_service">Приоритетное обслуживание</option><option value="commercial">Коммерческая отдача</option></select></label><label className="field">Режим<select value={mode} disabled={busy} onChange={e=>setMode(e.target.value as Mode)}><option value="centralized">Централизованный</option><option value="federated">Федеративный</option></select></label>{mode==='centralized'&&<label className="field">Планировщик<select value={planner} disabled={busy} onChange={e=>setPlanner(e.target.value)}><option value="economic">Экономический</option><option value="baseline">Простое правило</option></select></label>}<label className="field">Название<input value={label} disabled={busy} onChange={e=>setLabel(e.target.value)} placeholder="Необязательно" /></label><button disabled={busy||!scenarioId}>{run?'Новый расчёт':'Начать смену'}</button></form></section>
    {notice&&<div className="notice" role="status">{notice}</div>}
    {run&&<>
      <div className="kpi-grid"><Kpi label="Граница шага" value={`${run.step} / ${run.steps_total}`} note={run.finished?'Смена завершена':'Следующее решение на этом шаге'} /><Kpi label="Выручка" value={money(s.revenue_usd)} /><Kpi label="P3 выполнены в срок" value={s.critical_jobs_due?`${s.critical_jobs_completed_on_time} / ${s.critical_jobs_due}`:'—'} note="Из всех P3 с наступившим сроком" /><Kpi label="Просрочено" value={<span className="bad">{num(s.jobs_due_missed)}</span>} /><Kpi label="Минимальный заряд" value={`${num(s.minimum_soc_pct,1)}%`} /></div>
      <section className="panel"><div className="panel-heading"><h3>{run.label||run.scenario_id}</h3><span className="badge">{run.mode==='federated'?'Федеративный':'Централизованный'} · {objectiveName(run.objective)}</span></div><div className="step-progress"><span style={{width:`${100*run.step/run.steps_total}%`}} /></div><div className="toolbar"><button className="ghost" disabled={busy||run.finished} onClick={()=>void advance(Math.min(run.step+1,run.steps_total))}>Шаг +1</button><button className="ghost" disabled={busy||run.finished} onClick={()=>void advance(Math.min(run.step+12,run.steps_total))}>+12 шагов</button><label className="field" style={{maxWidth:145,flex:'0 1 145px'}}>Остановиться на шаге<input aria-label="Целевой шаг" type="number" min={run.step} max={run.steps_total} value={target} onChange={e=>setTarget(Number(e.target.value))} disabled={busy} /></label><button disabled={busy||run.finished||target<=run.step} onClick={()=>void advance(target)}>До шага {target}</button><button className="ghost" disabled={busy||run.finished} onClick={()=>void advance(run.steps_total)}>До конца смены</button>{running&&<button className="danger" onClick={()=>{stop.current=true;setNotice('Останавливаемся после текущего запроса, не более 12 шагов.')}}>Пауза</button>}<button className="ghost" disabled={busy} onClick={()=>void checkpoint()}>Сохранить точку</button></div><p className="muted" style={{fontSize:11,marginTop:15,marginBottom:0}}>Сохраните точку перед продолжением, чтобы позже сравнить варианты с этого состояния. Выполненные шаги не перематываются.</p></section>
      <div className="two-column"><section className="panel"><h3>Сообщения оператору → системе</h3><p className="muted" style={{fontSize:12}}>Одиночное событие подаётся на текущем шаге. Файл событий создаёт очередь для последующего выполнения.</p><label className="field">Файл событий JSON<input type="file" accept=".json,application/json" disabled={busy||run.finished} onChange={e=>{const file=e.target.files?.[0];e.target.value='';if(file)void task('Читаем файл событий…',async()=>setText(await file.text()))}} /></label><label className="field" style={{marginTop:12}}>JSON сообщения<textarea rows={5} value={text} disabled={busy||run.finished} onChange={e=>setText(e.target.value)} placeholder={'{"id":"E-01","at_step":72,"type":"satellite_outage","satellite_ids":["S01"],"end_step":90}'} /></label><button disabled={busy||run.finished||!text.trim()} style={{marginTop:12}} onClick={()=>void submitJSON()}>Подать JSON в {run.scenario_id}</button>{fileScenario && fileScenario!==run.scenario_id && <p className="notice warning" style={{marginTop:12}}>Файл помечен сценарием {fileScenario}. Вы подаёте его в текущий {run.scenario_id}; сервер проверит ID аппаратов и задания именно здесь. Это позволяет применить пример events_demo к P04 по сценарию приёмки.</p>}<details style={{marginTop:12}}><summary>Ручной ввод сообщения</summary><form onSubmit={e=>{e.preventDefault();void task('Передаём сообщение…',async()=>{await safely(run,async()=>{const result=await api.sendManualEvent(run.run_id,{type:eventType,satellite_ids:sats,end_step:end}) as {state:RunState};selectRun(result.state);setNotice(`Сообщение принято на шаге ${run.step}.`);setSats([])})})}}><div className="form-grid"><label className="field">Тип<select disabled={busy||run.finished} value={eventType} onChange={e=>setEventType(e.target.value as typeof eventType)}><option value="satellite_outage">Недоступность аппаратов</option><option value="close_downlink">Закрытие downlink</option></select></label><label className="field">Конец интервала, не включая<input type="number" min={run.step+1} max={run.steps_total} value={end} disabled={busy||run.finished} onChange={e=>setEnd(Number(e.target.value))} required /></label></div><p className="muted" style={{fontSize:11,marginTop:15}}>Аппараты · выбрано {sats.length}</p><div className="sat-choices">{Object.keys(run.satellites).map(id=><label key={id} className="sat-choice"><input type="checkbox" disabled={busy||run.finished} checked={sats.includes(id)} onChange={e=>setSats(old=>e.target.checked?[...old,id]:old.filter(v=>v!==id))} />{id}</label>)}</div><button className="ghost" style={{marginTop:12}} disabled={busy||run.finished||!sats.length}>Подать сообщение</button></form></details></section>
      <section className="panel"><h3>Цель и история вмешательств</h3><label className="field">Цель текущего расчёта<select value={run.objective} disabled={busy||run.finished} onChange={e=>{const value=e.target.value as Objective;void task('Меняем цель управления…',async()=>{await api.setObjective(run.run_id,value);selectRun(await api.run(run.run_id));await loadHistory(run.run_id);setNotice(`Цель изменена на границе шага ${run.step}.`)})}}><option value="priority_service">Приоритетное обслуживание</option><option value="commercial">Коммерческая отдача</option></select></label><h4>Смена цели</h4><div className="event-feed">{history?.run_metadata?.objective_changes?.map((change,i)=><div className="event-item" key={i}><span>Шаг {change.step}</span><span>{objectiveName(change.from)} → {objectiveName(change.to)}</span></div>)??<p className="muted">Цель ещё не менялась.</p>}</div><h4>Принятые сообщения · {history?.events.length??'—'}</h4><div className="event-feed">{history?.events.length?history.events.map(event=><div className="event-item" key={event.id}><span><b>{event.id}</b><br /><small>{eventName(event.type)}</small></span><span className="badge">Шаг {event.at_step}</span></div>):<p className="muted">Принятых сообщений пока нет.</p>}</div></section></div>
      {queue.length>0&&<section className="panel"><h3>Очередь будущих сообщений · {queue.length}</h3><p className="muted">Очередь хранится в этой вкладке и применяется при продолжении через пульт. Сервер проверит каждое сообщение на его шаге. Выгрузка содержит только уже принятые сообщения.</p><div className="event-feed">{queue.map(event=><div className="event-item" key={event.id}><span>{event.id} · {eventName(event.type)} · шаг {event.at_step}</span><button className="ghost" disabled={busy} onClick={()=>updateQueue(run.run_id,queued.current.filter(e=>e.id!==event.id))}>Убрать из очереди</button></div>)}</div></section>}
      <section className="panel"><div className="panel-heading"><h3>Состояние группировки</h3><Link to="/constellation">Матрица и история →</Link></div><div className="table-wrap table-scroll"><table><thead><tr><th>Аппарат</th><th>Заряд</th><th>Температура</th><th>Калибровка</th><th>Downlink-окно</th><th>Доступность</th><th>Последнее решение</th></tr></thead><tbody>{Object.entries(run.satellites).map(([id,st])=><tr key={id}><td className="mono">{id}</td><td>{num(st.soc_pct,1)}%</td><td>{num(st.temp_c,1)} °C</td><td>Ещё {st.calibration_valid_for} шагов</td><td className={st.in_contact?'ok':'muted'}>{st.in_contact?'В окне':'Вне окна'}</td><td className={!st.available?'bad':''}>{st.available?'Доступен':'Недоступен'}</td><td><button className="link" disabled={run.step===0} onClick={()=>openDecision(id,run.step-1)}>Разобрать ↗</button></td></tr>)}</tbody></table></div></section>
    </>}
  </>
}


