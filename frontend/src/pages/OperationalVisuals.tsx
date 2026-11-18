import { useEffect, useMemo, useRef, useState } from 'react'
import { num } from '../components/common'
import { operationNames, rowOperation, ScenarioJob, TraceRow } from './operationalData'

const colors: Record<string, string> = { idle: '#354455', calibrate: '#e8b064', relay: '#4bb7a1', downlink: '#779bef', job: '#69a5b6', future: '#1c2632' }
export function OperationMatrix({ rows, satellites, steps, totalSteps, jobs, selected, onSelect, heat = false }: { rows: TraceRow[]; satellites: string[]; steps: number; totalSteps: number; jobs: Map<string, ScenarioJob>; selected?: string; onSelect: (satellite: string, step: number) => void; heat?: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [hover, setHover] = useState('Наведите на ячейку: аппарат, шаг и операция. Нажмите для разбора.')
  const [keyboardSat, setKeyboardSat] = useState(satellites[0] ?? '')
  const [keyboardStep, setKeyboardStep] = useState(Math.max(0, steps - 1))
  const [themeVersion, setThemeVersion] = useState(0)
  const cell = totalSteps > 100 ? 6 : 12, left = 54, top = 27, rowHeight = 21
  const width = left + totalSteps * cell + 8, height = top + satellites.length * rowHeight + 8
  const lookup = useMemo(() => new Map(rows.map(row => [`${row.satellite_id}:${row.step}`, row])), [rows])
  useEffect(() => {
    const observer = new MutationObserver(() => setThemeVersion(value => value + 1))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-variant'] })
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    const node = canvas.current, ctx = node?.getContext('2d'); if (!node || !ctx) return
    const ratio = window.devicePixelRatio || 1
    node.width = width * ratio; node.height = height * ratio; ctx.scale(ratio, ratio)
    const style = getComputedStyle(node), muted = style.getPropertyValue('--muted').trim(), accent = style.getPropertyValue('--accent').trim()
    const palette = { ...colors, idle: style.getPropertyValue('--ops-idle').trim(), future: style.getPropertyValue('--ops-future').trim() }
    ctx.clearRect(0, 0, width, height); ctx.font = '10px Segoe UI'; ctx.textBaseline = 'middle'
    for (let k = 0; k < totalSteps; k += totalSteps > 100 ? 24 : 6) { ctx.fillStyle = muted; ctx.fillText(String(k), left + k * cell, 11) }
    satellites.forEach((sid, i) => {
      const y = top + i * rowHeight
      ctx.fillStyle = sid === selected ? accent : muted; ctx.fillText(sid, 2, y + rowHeight / 2)
      for (let step = 0; step < totalSteps; step++) {
        const row = lookup.get(`${sid}:${step}`), operation = row ? rowOperation(row, jobs) : 'future'
        ctx.fillStyle = heat && row ? row.executed === 'job' ? '#5ec7b1' : row.executed === 'calibrate' ? '#ba975d' : palette.idle : palette[operation as keyof typeof palette] ?? palette.idle
        ctx.fillRect(left + step * cell, y + 2, Math.max(1, cell - 1), rowHeight - 4)
        if (row && row.reason !== 'accepted' && row.reason !== 'idle') { ctx.fillStyle = '#f46c7a'; ctx.fillRect(left + step * cell, y + 2, cell - 1, 3) }
      }
    })
  }, [width, height, satellites, selected, totalSteps, rows, jobs, lookup, cell, heat, themeVersion])
  function locate(event: React.MouseEvent<HTMLCanvasElement>) { const rect = event.currentTarget.getBoundingClientRect(); return { step: Math.floor((event.clientX - rect.left - left) / cell), index: Math.floor((event.clientY - rect.top - top) / rowHeight) } }
  return <div className="ops-matrix"><div className="ops-legend">{['relay', 'downlink', 'calibrate', 'idle'].map(key => <span key={key}><i style={{ background: key === 'idle' ? 'var(--ops-idle)' : colors[key] }} />{operationNames[key]}</span>)}<span><i style={{ background: '#f46c7a' }} />Отказ модели</span><span>Будущие шаги не исполнены</span></div><div className="ops-canvas-scroll"><canvas ref={canvas} style={{ width, height }} role="img" aria-label={`Матрица ${satellites.length} аппаратов, ${steps} исполненных шагов. Разбор доступен также в форме под матрицей.`} onMouseMove={event => { const { step, index } = locate(event), sid = satellites[index], row = lookup.get(`${sid}:${step}`); if (sid && step >= 0 && step < totalSteps) setHover(`${sid} · шаг ${step} · ${row ? operationNames[rowOperation(row, jobs)] : 'ещё не исполнен'}`) }} onClick={event => { const { step, index } = locate(event), sid = satellites[index]; if (sid && lookup.has(`${sid}:${step}`)) onSelect(sid, step) }} /></div><div className="ops-matrix-footer"><span>{hover}</span><span>{num(rows.length)} / {num(steps * satellites.length)} строк истории</span></div><div className="toolbar ops-compact-toolbar"><label className="field">Аппарат<select value={satellites.includes(keyboardSat) ? keyboardSat : satellites[0]} onChange={e => setKeyboardSat(e.target.value)}>{satellites.map(sid => <option key={sid}>{sid}</option>)}</select></label><label className="field">Шаг<input type="number" min={0} max={Math.max(0, steps - 1)} value={keyboardStep} onChange={e => setKeyboardStep(Number(e.target.value))} /></label><button className="button" disabled={!steps || !Number.isInteger(keyboardStep) || keyboardStep < 0 || keyboardStep >= steps} onClick={() => onSelect(satellites.includes(keyboardSat) ? keyboardSat : satellites[0], keyboardStep)}>Разобрать решение</button></div></div>
}

export function ResourceChart({ values, title, unit, thresholds, color }: { values: { step: number; value: number }[]; title: string; unit: string; thresholds: { value: number; label: string; color: string }[]; color: string }) {
  if (!values.length) return <div className="empty">Нет истории ресурса.</div>
  const all = [...values.map(p => p.value), ...thresholds.map(t => t.value)], rawMin = Math.min(...all), rawMax = Math.max(...all)
  const padding = Math.max(2, (rawMax - rawMin) * .12), min = rawMin - padding, max = rawMax + padding
  const left = 43, top = 18, width = 580, height = 194, innerW = width - left - 14, innerH = height - top - 27
  const last = Math.max(1, values[values.length - 1].step), x = (step: number) => left + step / last * innerW, y = (value: number) => top + (max - value) / (max - min) * innerH
  return <div className="ops-resource-chart"><h4>{title} <span className="muted">{unit}</span></h4><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}. ${values.length} значений, пороги из сценария.`}>{[0, 1, 2, 3].map(tick => { const value = min + tick / 3 * (max - min); return <g key={tick}><line x1={left} x2={width - 14} y1={y(value)} y2={y(value)} stroke="var(--line)" /><text x={left - 7} y={y(value) + 3} textAnchor="end">{num(value, 1)}</text></g> })}{thresholds.map(t => <g key={t.label}><line x1={left} x2={width - 14} y1={y(t.value)} y2={y(t.value)} stroke={t.color} strokeDasharray="5 4" /><title>{t.label}: {t.value} {unit}</title></g>)}<polyline fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" points={values.map(v => `${x(v.step)},${y(v.value)}`).join(' ')} /><circle cx={x(values[values.length - 1].step)} cy={y(values[values.length - 1].value)} r="2.7" fill={color} /><text x={left} y={height - 5}>0</text><text x={width - 14} y={height - 5} textAnchor="end">граница {num(values[values.length - 1].step)}</text></svg><div className="ops-legend">{thresholds.map(t => <span key={t.label}><i style={{ background: t.color }} />{t.label}: {num(t.value, 1)} {unit}</span>)}</div></div>
}

export function TraceTable({ rows, jobs, onSelect, heading, note }: { rows: TraceRow[]; jobs: Map<string, ScenarioJob>; onSelect: (satellite: string, step: number) => void; heading: string; note?: string }) {
  const [page, setPage] = useState(0)
  useEffect(() => setPage(0), [rows])
  const pages = Math.max(1, Math.ceil(rows.length / 40)), safePage = Math.min(page, pages - 1)
  return <section className="panel ops-trace"><div className="ops-panel-heading"><div><h3>{heading}</h3>{note && <p className="muted">{note}</p>}</div><span className="badge">{num(rows.length)} записей</span></div>{rows.length ? <div className="table-wrap"><table><thead><tr><th>Шаг</th><th>Аппарат</th><th>Действие</th><th>Задание</th><th>Энергия, Вт·ч</th><th>Температура, °C</th><th>Ответ модели</th><th /></tr></thead><tbody>{rows.slice(safePage * 40, safePage * 40 + 40).map(row => <tr key={`${row.satellite_id}:${row.step}`}><td>{row.step}</td><td>{row.satellite_id}</td><td>{operationNames[rowOperation(row, jobs)]}</td><td>{row.requested.job_id ?? '—'}</td><td>{num(row.energy_before_wh, 1)} → {num(row.energy_after_wh, 1)}</td><td>{num(row.temp_before_c, 1)} → {num(row.temp_after_c, 1)}</td><td>{row.reason}</td><td><button className="button secondary" onClick={() => onSelect(row.satellite_id, row.step)}>Разбор</button></td></tr>)}</tbody></table></div> : <p className="empty">По выбранному условию нет исполненных операций.</p>}<div className="ops-pagination"><span>Страница {safePage + 1} / {pages}</span><button className="button secondary" disabled={!safePage} onClick={() => setPage(safePage - 1)}>←</button><button className="button secondary" disabled={safePage >= pages - 1} onClick={() => setPage(safePage + 1)}>→</button></div></section>
}
