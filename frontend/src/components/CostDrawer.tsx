import { useEffect, useRef, useState } from 'react'
import { api, ServiceError } from '../api/client'
import { ErrorNotice, Loading, num, money } from './common'
import './flow.css'

interface CostCard {
  satellite_id: string; marginal_energy_wh?: number; energy_wh: number; full_load_energy_wh?: number
  ground_slots: number; sat_steps: number; thermal_rise_c: number; calibration_share: number
  energy_pct_of_usable: number | null; feasible: boolean; blocking: string | null
}
interface CostResult { job: { id: string; work_steps: number; value_usd: number }; cost_cards: CostCard[] }
const cache = new Map<string, CostResult>()

export default function CostDrawer({ scenarioId, jobId, onClose }: { scenarioId: string; jobId: string; onClose: () => void }) {
  const key = `${scenarioId}\0${jobId}`
  const [snapshot, setSnapshot] = useState<{ key: string; data: CostResult } | null>(() => cache.has(key) ? { key, data: cache.get(key)! } : null)
  const result = snapshot?.key === key ? snapshot.data : cache.get(key) ?? null
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(!cache.has(key))
  const close = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const oldFocus = document.activeElement as HTMLElement | null
    close.current?.focus()
    return () => oldFocus?.focus()
  }, [])
  useEffect(() => {
    let active = true
    setSnapshot(cache.has(key) ? { key, data: cache.get(key)! } : null); setError(null); setLoading(!cache.has(key))
    if (!cache.has(key)) void api.jobCost(encodeURIComponent(scenarioId), encodeURIComponent(jobId)).then(raw => {
      const response = raw as CostResult
      cache.set(key, response)
      if (cache.size > 100) cache.delete(cache.keys().next().value as string)
      if (active) setSnapshot({ key, data: response })
    }).catch(failure => { if (active) setError(failure) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [key, scenarioId, jobId])
  return <div className="flow-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}><aside className="flow-drawer" role="dialog" aria-modal="true" aria-labelledby="cost-heading" onKeyDown={e => { if (e.key === 'Escape') onClose() }}>
    <header className="flow-drawer-head"><div><span className="muted">Карта ресурсных затрат</span><h2 id="cost-heading">{jobId}</h2></div><button ref={close} className="button" onClick={onClose} aria-label="Закрыть карту затрат">✕</button></header>
    <p className="muted">Исходный сценарий {scenarioId}. Полный объём задания и начальный запас энергии; это не баланс текущего шага и не денежная себестоимость.</p>
    {loading && <Loading text="Получаем карты допустимых исполнителей…" />}
    <ErrorNotice error={error} />
    {error instanceof ServiceError && <p className="muted">Карты доступны только для заданий исходного сценария. Для задач, полученных событием add_jobs, отдельная карта текущего запуска в API отсутствует. Значения не подставляются из другой задачи.</p>}
    {result && <><div className="kpi-grid"><div className="kpi"><span>Работа</span><strong>{num(result.job.work_steps)} шаг.</strong></div><div className="kpi"><span>Выручка после завершения</span><strong>{money(result.job.value_usd)}</strong></div></div>
      <div className="table-wrap"><table className="flow-cost-table"><thead><tr><th>Аппарат</th><th>Сверх ожидания, Вт·ч</th><th>Полная нагрузка, Вт·ч</th><th>Слоты Земли</th><th>Шаги аппарата</th><th>Сдвиг T равнов., °C</th><th>Запас / предупреждение</th></tr></thead><tbody>{result.cost_cards.map(card => <tr key={card.satellite_id}><td>{card.satellite_id}</td><td>{num(card.marginal_energy_wh ?? card.energy_wh, 3)}</td><td>{num(card.full_load_energy_wh, 3)}</td><td>{num(card.ground_slots)}</td><td>{num(card.sat_steps)}</td><td>{num(card.thermal_rise_c, 2)}</td><td>{card.energy_pct_of_usable === null ? 'Запаса над резервом нет' : `${num(card.energy_pct_of_usable, 1)}% доступной энергии`}{card.blocking && <div className="badge">{card.blocking === 'energy_tight' ? 'Тесный энергобюджет; возможна дозарядка' : card.blocking}</div>}</td></tr>)}</tbody></table></div>
      <p className="muted">Полная нагрузка не учитывает солнце и нагреватель. Температура — сдвиг равновесия, а не фактический нагрев за задание. Предупреждение energy_tight не доказывает невыполнимость: окончательный допуск определяет модель.</p></>}
  </aside></div>
}
