import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useWorkspace } from '../workspace'
import { ErrorNotice, Loading, num } from './common'
import './flow.css'

interface Candidate { job_id?: string; net_value?: number; reason?: string; admitted?: boolean; margin?: number; detail?: unknown }
interface Explanation {
  step: number; satellite_id: string; available?: boolean; message?: string
  requested?: { action: string; job_id?: string }; executed?: string
  reason_code?: string; reason_text?: string; model_reason?: string; score?: number; mismatch?: boolean; detail_available?: boolean
  considered?: { top_candidates?: Candidate[]; shadow_prices?: { ground_slot?: number; energy_wh?: number; sat_step?: number }; rejected?: Candidate[]; net_value?: number; detail?: unknown; recomputed?: boolean; truncated?: boolean; source?: string; skipped?: Candidate[]; abandoned?: Candidate[]; [key: string]: unknown }
  consequences?: { energy_before_wh: number; energy_after_wh: number; temp_before_c: number; temp_after_c: number; load_w: number; heater_w: number; calibration_age_steps: number; completed_job: string | null; below_reserve: boolean; brownout: boolean }
}
const cached = new Map<string, Explanation>()
const pending = new Map<string, Promise<Explanation>>()
const actionNames: Record<string, string> = { idle: 'Ожидание', calibrate: 'Калибровка', job: 'Работа над заданием' }
const reasons: Record<string, string> = {
  accepted: 'Операция разрешена', idle: 'Ожидание разрешено', energy_reserve: 'Недостаточный запас энергии', thermal_limit: 'Температурный предел', calibration_required: 'Требуется калибровка', no_contact: 'Нет контакта', satellite_unavailable: 'Аппарат недоступен', ground_capacity: 'Нет свободного слота наземного сегмента', duplicate_job_in_step: 'Задание уже назначено в этом шаге', outside_job_window: 'Вне временного окна', already_completed: 'Задание уже завершено', ineligible_satellite: 'Недопустимый исполнитель', infeasible_by_contacts: 'Недостаточно контактных окон', infeasible_by_deadline: 'Не хватает шагов до срока', no_completion_margin: 'Недостаточный запас до срока', energy_forecast_short: 'Прогноз энергии не покрывает работу', negative_value: 'Ресурс уступает более ценному применению', min_soc_pct: 'Ниже порога заряда бортового задания', preempted_by: 'Вытеснено другим заданием', expired: 'Истёк срок действия', no_window: 'Нет допустимого окна',
}
function readExplanation(runId: string, step: number, satelliteId: string, key: string, full = false): Promise<Explanation> {
  const known = cached.get(key)
  // Готовый мелкий ответ не мешает дозапросу глубокой части.
  if (known && (!full || known.detail_available !== false)) return Promise.resolve(known)
  const slot = full ? key + '#full' : key
  const inflight = pending.get(slot)
  if (inflight) return inflight
  const request = api.explain(encodeURIComponent(runId), step, encodeURIComponent(satelliteId), full).then(value => {
    const result = value as Explanation
    // Сохраняем только исполненную историю; ответ «ещё не принято» не кешируется.
    if (result.executed !== undefined) {
      cached.set(key, result)
      if (cached.size > 160) cached.delete(cached.keys().next().value as string)
    }
    return result
  }).finally(() => pending.delete(slot))
  pending.set(slot, request)
  return request
}
function Candidates({ rows, rejection = false }: { rows: Candidate[]; rejection?: boolean }) {
  return <div className="table-wrap"><table className="flow-cost-table"><thead><tr><th>Задание</th><th>Чистая оценка NV</th>{rejection && <th>Почему исключено</th>}</tr></thead><tbody>{rows.map((row, i) => <tr key={`${row.job_id}:${i}`}><td>{row.job_id ?? '—'}</td><td>{num(row.net_value, 4)}</td>{rejection && <td>{reasons[row.reason ?? ''] ?? row.reason ?? 'Причина не передана'}{row.detail != null && <details><summary>Детали ограничения</summary><pre>{JSON.stringify(row.detail, null, 2)}</pre></details>}</td>}</tr>)}</tbody></table></div>
}

export default function ExplainDrawer() {
  const { decision, closeDecision } = useWorkspace()
  const key = decision ? `${decision.runId}:${decision.step}:${decision.satelliteId}` : ''
  const [current, setCurrent] = useState<{ key: string; value: Explanation } | null>(null)
  const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null)
  const [attempt, setAttempt] = useState(0)
  // Дозапрос глубокой части: только по кнопке, по одному шагу за раз.
  const [deep, setDeep] = useState<{ key: string; busy: boolean; error: unknown } | null>(null)
  const close = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (!decision) return
    let active = true
    setFailure(null)
    setDeep(null)
    void readExplanation(decision.runId, decision.step, decision.satelliteId, key).then(value => { if (active) setCurrent({ key, value }) }).catch(error => { if (active) setFailure({ key, error }) })
    return () => { active = false }
  }, [key, attempt])
  useEffect(() => {
    if (!decision) return
    const focus = document.activeElement as HTMLElement | null
    close.current?.focus()
    return () => focus?.focus()
  }, [Boolean(decision)])
  if (!decision) return null
  const data = cached.get(key) ?? (current?.key === key ? current.value : null)
  const error = failure?.key === key ? failure.error : null
  const considered = data?.considered, consequences = data?.consequences
  const alternatives = considered?.top_candidates
  const rejected = considered?.rejected ?? considered?.skipped
  const prices = considered?.shadow_prices
  const deepBusy = deep?.key === key && deep.busy
  const deepError = deep?.key === key ? deep.error : null
  const requestDeep = () => {
    if (!decision || deepBusy) return
    setDeep({ key, busy: true, error: null })
    void readExplanation(decision.runId, decision.step, decision.satelliteId, key, true)
      .then(value => { setCurrent({ key, value }); setDeep({ key, busy: false, error: null }) })
      .catch(error => setDeep({ key, busy: false, error }))
  }
  // Глубокая часть у старого шага не хранится (З6). Кнопка — требование
  // контракта: автоматический full=true на каждом открытии сорвал бы О3.
  const deepNotice = data?.requested && data.detail_available === false
    ? <div className="notice"><strong>Глубокая часть этого шага не хранится</strong>
        <p>Отклонённые кандидаты и карта стоимости у шагов старше последних сохранённых выброшены ради памяти. Сервер восстановит их повторным расчётом смены — до пяти секунд на P04_demand, повторное открытие того же шага мгновенное.</p>
        <button className="button" onClick={requestDeep} disabled={deepBusy}>{deepBusy ? 'Восстанавливаем…' : 'Разобрать подробно'}</button>
        {deepBusy && <Loading text="Сервер повторяет расчёт смены до этого шага…" />}
        <ErrorNotice error={deepError} />
      </div>
    : null
  return <div className="flow-overlay" onMouseDown={e => { if (e.target === e.currentTarget) closeDecision() }}><aside className="flow-drawer" role="dialog" aria-modal="true" aria-labelledby="explain-heading" onKeyDown={e => { if (e.key === 'Escape') closeDecision() }}>
    <header className="flow-drawer-head"><div><span className="muted">Разбор исполненного решения</span><h2 id="explain-heading">{decision.satelliteId} · шаг {decision.step}</h2></div><button ref={close} className="button" onClick={closeDecision} aria-label="Закрыть разбор решения">✕</button></header>
    {!data && !error && <Loading text="Восстанавливаем основания решения. Панель уже открыта; старый шаг может потребовать повторного расчёта…" />}
    <ErrorNotice error={error} />
    {Boolean(error) && <button className="button" onClick={() => setAttempt(value => value + 1)}>Повторить запрос</button>}
    {data?.available === false && <div className="empty">{data.message ?? 'Решение для этого шага ещё не принято.'}</div>}
    {data?.requested && <>
      {data.mismatch === true && <div className="notice error"><strong>Запрошенное действие не исполнено</strong><p>Модель выполнила «{actionNames[data.executed ?? ''] ?? data.executed}». Причина допуска показана отдельно ниже.</p></div>}
      <section className="flow-section"><h3>Что выбрано</h3><div className="flow-detail-grid"><span>Запрошено</span><strong>{actionNames[data.requested.action] ?? data.requested.action}{data.requested.job_id && ` · ${data.requested.job_id}`}</strong><span>Исполнено</span><strong>{data.executed ? actionNames[data.executed] ?? data.executed : 'Нет строки исполнения'}</strong><span>Оценка score</span><strong>{num(data.score, 4)}</strong><span>Чистая оценка net_value</span><strong>{num(considered?.net_value, 4)}</strong></div></section>
      <section className="flow-section"><h3>{data.requested.action === 'idle' ? 'Причина выбора ожидания' : 'Основание выбора планировщика'}</h3><p>{data.reason_text ?? data.reason_code ?? 'Не передано'}</p><code>{data.reason_code}</code>{considered?.is_gateway === true && <p className="flow-warning">Аппарат сохраняет канал связи с Землёй.</p>}</section>
      <section className="flow-section"><h3>Отдельно: допуск эталонной модели</h3><p>{reasons[data.model_reason ?? ''] ?? data.model_reason ?? 'Проверка исполнения ещё не записана'}</p>{data.model_reason && <code>{data.model_reason}</code>}<p className="muted">Отказ операции на этом шаге не доказывает срыв срока задания. Причины пропуска срока анализируются отдельно в потоке и аналитике.</p></section>
      <section className="flow-section"><h3>Альтернативы, рассмотренные планировщиком</h3>{alternatives?.length ? <Candidates rows={alternatives} /> : <p className="muted">{alternatives ? 'Список кандидатов пуст.' : 'Этот планировщик не передал ранжированный список альтернатив.'}</p>}<p className="muted">API возвращает короткий список, а не все задания смены. Оценка NV — внутренняя оценка привлекательности с учётом ресурсов и срочности, не фактическая прибыль.</p></section>
      <section className="flow-section"><h3>Теневые цены ресурсов</h3>{prices ? <div className="flow-price-grid"><div><span>Слот наземного сегмента</span><strong>{num(prices.ground_slot, 4)}</strong><code>ground_slot</code></div><div><span>1 Вт·ч энергии</span><strong>{num(prices.energy_wh, 6)}</strong><code>energy_wh</code></div><div><span>Шаг аппарата</span><strong>{num(prices.sat_step, 4)}</strong><code>sat_step</code></div></div> : <p className="muted">Теневые цены в ответе этого планировщика отсутствуют. Нули вместо отсутствующих значений не подставляются.</p>}<p className="muted">Это оценка ценности дефицитного ресурса, а не тариф или денежные расходы аппарата.</p></section>
      <section className="flow-section"><h3>Исключённые кандидаты</h3>{rejected?.length ? <Candidates rows={rejected} rejection /> : <p className="muted">{rejected ? 'Исключённых в возвращённой выборке нет.' : data.detail_available === false ? 'Список исключений не хранится для этого шага — восстановите его кнопкой выше.' : 'Список исключений не предоставлен.'}</p>}{deepNotice}{considered?.abandoned?.length ? <><h4>Отложенные бортом задания</h4><Candidates rows={considered.abandoned} rejection /></> : null}</section>
      <section className="flow-section"><h3>Фактические последствия шага</h3>{consequences ? <><div className="flow-detail-grid"><span>Энергия, Вт·ч</span><strong>{num(consequences.energy_before_wh, 3)} → {num(consequences.energy_after_wh, 3)}</strong><span>Температура, °C</span><strong>{num(consequences.temp_before_c, 3)} → {num(consequences.temp_after_c, 3)}</strong><span>Полная нагрузка, Вт</span><strong>{num(consequences.load_w, 2)}</strong><span>Нагреватель, Вт</span><strong>{num(consequences.heater_w, 2)}</strong><span>Возраст калибровки после шага</span><strong>{num(consequences.calibration_age_steps)}</strong><span>Завершённое задание</span><strong>{consequences.completed_job ?? 'Нет'}</strong><span>Ниже резерва</span><strong>{consequences.below_reserve ? 'Да' : 'Нет'}</strong><span>Brownout</span><strong>{consequences.brownout ? 'Да' : 'Нет'}</strong></div>{consequences.below_reserve && <p className="flow-warning">Ожидание также расходует энергию: падение ниже резерва и отклонение команды — разные показатели.</p>}</> : <p className="muted">Фактические последствия не предоставлены.</p>}</section>
      {considered?.recomputed && <p className="muted">Полный разбор старого шага восстановлен сервером повторным расчётом.</p>}
      <details className="flow-section"><summary>Полный ответ и дополнительные сведения</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>
    </>}
  </aside></div>
}
