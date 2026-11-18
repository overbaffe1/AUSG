import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { EmptyRun, PageHeading, money, num } from '../components/common'
import { useWorkspace } from '../workspace'
import { downloadText, recordOf, reportHtml } from './compareExportData'
import './compare-export.css'

interface Verification { reproduced: boolean; steps_executed: number; summary: Record<string, unknown>; mismatches: Record<string, { saved: unknown; replayed: unknown }>; message: string }
export default function ExportPage() {
  const { run, busy, task } = useWorkspace()
  const [includeTrace, setIncludeTrace] = useState(false)
  const [report, setReport] = useState('')
  const [reportName, setReportName] = useState('')
  const [reportReady, setReportReady] = useState(false)
  const [showRaw, setShowRaw] = useState(false)
  const [raw, setRaw] = useState('')
  const [fileName, setFileName] = useState('')
  const [fileBytes, setFileBytes] = useState(0)
  const [verification, setVerification] = useState<Verification | null>(null)
  const [message, setMessage] = useState('')
  const [preparedDownload, setPreparedDownload] = useState<{ url: string; name: string } | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const reportFrame = useRef<HTMLIFrameElement>(null)
  useEffect(() => { setReport(''); setReportReady(false); setMessage(''); setPreparedDownload(null) }, [run?.run_id, run?.state_digest])
  useEffect(() => () => { if (preparedDownload) URL.revokeObjectURL(preparedDownload.url) }, [preparedDownload])
  const exportJson = () => {
    if (!run) return
    void task('Готовим JSON-выгрузку…', async () => {
      const text = await api.exportRunText(run.run_id, includeTrace)
      const name = `${run.scenario_id}_${run.run_id}.json`
      setPreparedDownload({ url: URL.createObjectURL(new Blob([text], { type: 'application/json;charset=utf-8' })), name })
      downloadText(text, name, 'application/json;charset=utf-8')
      setMessage(`Выгрузка подготовлена${includeTrace ? ' с журналом' : ' без журнала trace'}. Размер: ${num(new Blob([text]).size / 1024, 1)} КБ.`)
    })
  }
  const prepareReport = () => {
    if (!run) return
    void task('Готовим читаемый отчёт…', async () => {
      const fresh = await api.run(run.run_id)
      const record = recordOf(await api.exportRun(run.run_id, false))
      const after = await api.run(run.run_id)
      if (fresh.state_digest !== after.state_digest || record.steps_executed !== fresh.step) throw new Error('Расчёт изменился во время подготовки. Остановите его и повторите запрос отчёта.')
      const html = reportHtml(fresh, record)
      if (html !== report) setReportReady(false)
      setReport(html); setReportName(`${fresh.scenario_id}_${fresh.run_id}.html`)
    })
  }
  const loadFile = (file?: File) => {
    if (!file) return
    void task('Читаем файл результата…', async () => {
      setVerification(null)
      const text = await file.text()
      setRaw(text); setFileName(file.name); setFileBytes(file.size); setShowRaw(false)
    })
  }
  const verify = () => void task('Повторяем расчёт и сверяем показатели…', async () => {
    setVerification(null)
    if (!raw.trim()) throw new Error('Выберите JSON-файл расчёта или вставьте его содержимое.')
    // Разбор здесь проверяет форму. На сервер отправляется исходный текст без пересериализации чисел.
    let shape: { schema_version?: string; summary?: unknown }
    try { shape = JSON.parse(raw.replace(/^\uFEFF/, '')) } catch { throw new Error('Не удалось прочитать JSON. Выберите исходную выгрузку результата.') }
    if (!shape || shape.schema_version !== 'cosmo-B-ops-result-1.0') throw new Error('Нужна выгрузка cosmo-B-ops-result-1.0. Файл сценария или сообщений не подходит.')
    if (!shape.summary || typeof shape.summary !== 'object' || Array.isArray(shape.summary) || !Object.keys(shape.summary).length) throw new Error('В файле нет сохранённой сводки для сравнения. Используйте полную выгрузку расчёта.')
    const value = await api.importRunText(raw) as Verification
    if (!value || typeof value.reproduced !== 'boolean' || !value.summary || !value.mismatches) throw new Error('Сервер не вернул результат проверки воспроизведения.')
    setVerification(value)
  })
  return <div className="cx-page"><PageHeading title="Выгрузка и воспроизведение" subtitle="Сохраните команды, сообщения и результат. Повторный расчёт проверяется эталонной моделью." />
    {preparedDownload && <div className="notice success">Файл готов. Если загрузка не началась автоматически: <a href={preparedDownload.url} download={preparedDownload.name}>Скачать подготовленный JSON</a>.</div>}
    {run ? <section className="card"><div className="section-heading"><div><h3>Текущий расчёт</h3><p className="muted">{run.label || run.scenario_id} · {run.run_id} · исполнено {run.step} / {run.steps_total} шагов</p></div></div><div className="cx-two-columns"><div className="cx-export-option"><h4>Машиночитаемый JSON</h4><p className="muted">Исходный сценарий, полученные события, выполненные команды, сводка и параметры алгоритма.</p><label className="cx-checkbox"><input type="checkbox" checked={includeTrace} onChange={e => setIncludeTrace(e.target.checked)} disabled={busy} />Включить подробный журнал trace</label><p className="muted">На P04 журнал может занимать десятки мегабайт. Без него команды и сообщения остаются, воспроизведение доступно.</p><button onClick={exportJson} disabled={busy}>Сохранить расчёт · JSON</button></div><div className="cx-export-option"><h4>Читаемый отчёт</h4><p className="muted">Показатели, состояния аппаратов, полученные сообщения и параметры. Можно сохранить HTML или распечатать.</p><button className="ghost" onClick={prepareReport} disabled={busy}>Подготовить отчёт</button>{report && <div className="actions cx-report-actions"><button className="ghost" disabled={busy} onClick={() => downloadText(report, reportName, 'text/html;charset=utf-8')}>Скачать HTML</button><button className="ghost" disabled={busy || !reportReady} onClick={() => { reportFrame.current?.contentWindow?.focus(); reportFrame.current?.contentWindow?.print() }}>Печать отчёта</button></div>}</div></div>{message && <p className="notice success" role="status">{message}</p>}{report && <details className="cx-report-preview" open><summary>Предпросмотр отчёта</summary><iframe ref={reportFrame} title="Читаемый отчёт расчёта" srcDoc={report} onLoad={() => setReportReady(true)} sandbox="allow-same-origin allow-modals" /></details>}</section> : <EmptyRun />}
    <section className="card"><h3>Проверить сохранённый расчёт</h3><p className="muted">Загрузите JSON-выгрузку. Проверка повторит команды и полученные сообщения, затем сравнит сохранённую сводку с вычисленной. Текущая смена не меняется.</p><div className="cx-file-zone"><input ref={fileInput} type="file" accept=".json,application/json" disabled={busy} onChange={e => { loadFile(e.target.files?.[0]); e.target.value = '' }} aria-label="Выбрать JSON-выгрузку расчёта" />{fileName && <span>{fileName} · {num(fileBytes / 1024, 1)} КБ</span>}</div><details className="cx-raw-input" open={showRaw} onToggle={e => setShowRaw(e.currentTarget.open)}><summary>Вставить или просмотреть JSON</summary>{showRaw && <label>Содержимое выгрузки<textarea rows={8} value={raw} disabled={busy} spellCheck={false} onChange={e => { setRaw(e.target.value); setFileName('Вставленный JSON'); setFileBytes(new Blob([e.target.value]).size); setVerification(null) }} /></label>}</details><div className="actions"><button onClick={verify} disabled={busy || !raw.trim()}>Проверить воспроизведение</button>{raw && <button className="ghost" disabled={busy} onClick={() => { setRaw(''); setFileName(''); setFileBytes(0); setVerification(null) }}>Очистить</button>}</div><p className="cx-import-note muted">Импорт здесь проверяет файл и не регистрирует новый запуск. Продолжение импортированного расчёта на пульте не поддерживается. Журнал trace и хеш сценария сервер отдельно не сверяет.</p>
      {verification && <section className={`cx-verification ${verification.reproduced ? 'success' : 'failure'}`} role="status"><span className="eyebrow">Результат проверки</span><h3>{verification.reproduced ? 'Расчёт воспроизведён, показатели совпали' : 'Расчёт повторён, но показатели расходятся'}</h3><p>{verification.message}</p><div className="cx-verification-metrics"><div><span>Исполнено шагов</span><strong>{num(verification.steps_executed)}</strong></div><div><span>Выручка</span><strong>{money(verification.summary.revenue_usd)}</strong></div><div><span>P3 в срок / наступил срок</span><strong>{num(verification.summary.critical_jobs_completed_on_time)} / {num(verification.summary.critical_jobs_due)}</strong></div></div>{Object.keys(verification.mismatches).length > 0 && <div className="table-scroll"><table><thead><tr><th>Показатель</th><th>В файле</th><th>При повторении</th></tr></thead><tbody>{Object.entries(verification.mismatches).map(([key, values]) => <tr key={key}><td>{key}</td><td><code>{JSON.stringify(values.saved)}</code></td><td><code>{JSON.stringify(values.replayed)}</code></td></tr>)}</tbody></table></div>}<details><summary>Вычисленная сводка</summary><pre>{JSON.stringify(verification.summary, null, 2)}</pre></details></section>}
    </section>
  </div>
}
