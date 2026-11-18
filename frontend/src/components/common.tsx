import { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ServiceError } from '../api/client'
export const num = (value: unknown, digits = 0) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—'
export const money = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${num(value, 2)} $` : '—'
export const percent = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${num(value * 100, 1)}%` : '—'
export function PageHeading({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) { return <header className="page-heading"><div><h2>{title}</h2>{subtitle && <p className="muted">{subtitle}</p>}</div>{actions && <div className="actions">{actions}</div>}</header> }
export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof Error ? error.message : String(error)
  return <div className="notice error" role="alert"><strong>Не удалось выполнить запрос</strong><p>{message}</p>{error instanceof ServiceError && Object.keys(error.details).length > 0 && <details><summary>Подробности</summary><pre>{JSON.stringify(error.details, null, 2)}</pre></details>}</div>
}
export function EmptyRun() { return <section className="empty-state"><span className="empty-symbol">◌</span><h3>Сначала откройте смену</h3><p>Создайте расчёт на пульте или выберите сохранённый в верхней панели.</p><Link className="button" to="/shift">К пульту смены →</Link></section> }
export function Loading({ text = 'Загружаем данные…' }: { text?: string }) { return <div className="loading-state" role="status"><span className="spinner" />{text}</div> }
export function Kpi({ label, value, note, onClick }: { label: string; value: ReactNode; note?: string; onClick?: () => void }) { const body = <><span className="label">{label}</span><strong className="value">{value}</strong>{note && <small>{note}</small>}</>; return onClick ? <button className="kpi metric-button" onClick={onClick}>{body}</button> : <div className="kpi">{body}</div> }
