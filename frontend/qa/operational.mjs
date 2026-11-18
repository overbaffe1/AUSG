// Запуск: node qa/operational.mjs. Использует отдельные тестовые расчёты и удаляет их после проверки.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const base = process.env.OPERATIONS_API ?? 'http://127.0.0.1:5018'
const created = []
async function request(path, payload, method) {
  const response = await fetch(base + path, { method: method ?? (payload ? 'POST' : 'GET'), headers: { 'Content-Type': 'application/json' }, ...(payload ? { body: JSON.stringify(payload) } : {}) })
  const result = await response.json()
  if (!response.ok) throw new Error(JSON.stringify(result))
  return result
}
const windows = []
const source = readFileSync(new URL('../src/pages/operationalData.ts', import.meta.url), 'utf8').replace(/^import .*$/gm, '')
const context = vm.createContext({ exports: {}, api: { trace: async (id, params) => { windows.push(params); return request(`/api/runs/${id}/trace?${new URLSearchParams(params)}`) } } })
vm.runInContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, context)
const { loadCompleteTrace, contactHistory, rowOperation } = context.exports

try {
  const initial = await request('/api/runs', { scenario_id: 'P04_demand', objective: 'commercial', mode: 'centralized', planner: 'economic', label: 'QA operational visualisations' })
  created.push(initial.run_id)
  const empty = await request(`/api/runs/${initial.run_id}/analytics`)
  assert.equal(empty.priority_service.share, null, 'Доля P3 до наступления срока должна оставаться null')
  assert.equal((await loadCompleteTrace(initial.run_id, 0, 48)).length, 0)
  const run = (await request(`/api/runs/${initial.run_id}/advance`, { to_end: true })).state
  const [rows, exported, analytics] = await Promise.all([
    loadCompleteTrace(run.run_id, run.step, Object.keys(run.satellites).length),
    request(`/api/runs/${run.run_id}/export?include_trace=false`),
    request(`/api/runs/${run.run_id}/analytics`),
  ])
  assert.equal(run.step, 288)
  assert.equal(rows.length, 13824)
  assert.equal(new Set(rows.map(row => `${row.satellite_id}:${row.step}`)).size, 13824)
  assert.deepEqual(windows.map(w => [w.from_step, w.to_step]), [[0, 95], [96, 191], [192, 287]])
  assert.equal(exported.trace, undefined)
  assert.equal(exported.initial_scenario.model.reserve_soc_pct, 30)
  const jobs = new Map(exported.initial_scenario.jobs.map(job => [job.id, job]))
  const data = { rows, exported, analytics, jobs }
  const contacts = contactHistory(data)
  assert.equal(contacts.downlink.length, 288)
  assert.deepEqual(Array.from(contacts.downlink.flatMap((count, step) => count === 0 ? [step] : [])), [224, 230])
  const jobsExecuted = rows.filter(row => row.executed === 'job').length
  assert.equal(Object.values(analytics.utilisation).reduce((sum, sat) => sum + sat.job_steps, 0), jobsExecuted)
  assert.equal(rows.filter(row => rowOperation(row, jobs) === 'downlink').length, analytics.ground_link.slots_used)
  const lastExplanation = await request(`/api/runs/${run.run_id}/explain?step=287&satellite_id=S01`)
  assert.ok(lastExplanation.consequences)
  assert.ok(typeof lastExplanation.reason_code === 'string')
  const counts = new Map()
  for (const row of rows) counts.set(row.satellite_id, (counts.get(row.satellite_id) ?? 0) + 1)
  assert.ok([...counts.values()].every(count => count === 288))
  console.log(`P04 passed: ${rows.length} unique rows, 3 inclusive windows; ${jobsExecuted} job steps; ground slots ${analytics.ground_link.slots_used}.`)

  const onboard = await request('/api/runs', { scenario_id: 'P01_intro', mode: 'federated', planner: 'economic', label: 'QA bundle shapes' })
  created.push(onboard.run_id)
  await request(`/api/runs/${onboard.run_id}/advance`, { to_step: 6 })
  const bundles = await request(`/api/runs/${onboard.run_id}/bundles`)
  assert.ok(Array.isArray(bundles.bundles) && bundles.bundles.length > 0)
  assert.ok(Array.isArray(bundles.uplink.uploads))
  for (const bundle of bundles.bundles) {
    assert.equal(typeof bundle.satellite_id, 'string')
    assert.ok(Array.isArray(bundle.gateway_steps))
    assert.ok(Array.isArray(bundle.queue))
    assert.ok(bundle.valid_until_step > bundle.issued_at_step)
  }
  console.log(`Federated passed: ${bundles.bundles.length} bundles, ${bundles.uplink.uploads.length} uplink records.`)
} finally {
  await Promise.all(created.map(id => request(`/api/runs/${id}`, undefined, 'DELETE').catch(error => console.error('Cleanup:', error.message))))
}
