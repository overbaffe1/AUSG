// Запуск: node qa/flow-smoke.mjs [http://127.0.0.1:5018]
// Проверяет реальный контракт. Создаёт и затем удаляет только свои QA-прогоны.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import ts from 'typescript'

const base = process.argv[2] ?? 'http://127.0.0.1:5018'
const originalFetch = globalThis.fetch
const calls = []
globalThis.fetch = async (path, options) => {
  calls.push(String(path))
  return originalFetch(new URL(path, base), options)
}
const compile = async path => ts.transpileModule(await fs.readFile(new URL(path, import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
const url = code => 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
const client = url(await compile('../src/api/client.ts'))
const helper = (await compile('../src/pages/flowData.ts')).replace(/from ['"]\.\.\/api\/client['"]/, `from '${client}'`)
const { loadFlow, reconstructFlow } = await import(url(helper))
const { api } = await import(client)
const owned = []
const request = async (path, body) => {
  const response = await fetch(path, body === undefined ? undefined : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const value = await response.json()
  assert.ok(response.ok, JSON.stringify(value))
  return value
}
try {
  const p04 = await api.createRun({ scenario_id: 'P04_demand', objective: 'commercial', mode: 'centralized', planner: 'economic', label: 'QA потока: восстановление P04' })
  owned.push(p04.run_id)
  const initial = await loadFlow(p04.run_id)
  assert.equal(initial.rows.length, 8120)
  assert.equal(initial.counts.waiting, 8120)
  assert.equal(initial.reconstructed, true)
  const result = await api.advance(p04.run_id, { to_end: true })
  const flow = await loadFlow(p04.run_id)
  const reference = await request(`/api/runs/${p04.run_id}/flow?limit=5000`)
  assert.equal(flow.step, result.state.step)
  assert.equal(flow.rows.length, 8120)
  assert.deepEqual(flow.counts, reference.counts)
  assert.ok(flow.counts.completed > 5000, `Для проверки >5000 нужна соответствующая корзина: ${JSON.stringify(flow.counts)}`)
  assert.equal(reference.jobs.completed.length, 5000)
  const lookup = new Map(flow.rows.map(job => [job.job_id, job]))
  for (const [status, jobs] of Object.entries(reference.jobs)) for (const job of jobs) {
    const actual = lookup.get(job.job_id)
    for (const field of ['remaining_steps', 'completed_step', 'work_steps']) assert.equal(actual[field], job[field])
    assert.equal(actual.status, status)
    assert.ok(Math.abs(actual.progress - job.progress) <= 0.00005)
  }
  const exported = await api.exportRun(p04.run_id, true)
  assert.throws(() => reconstructFlow({ ...exported, trace: exported.trace.slice(1) }), /неполная/)
  const firstRow = exported.trace.find(row => row.executed === 'job')
  assert.ok(firstRow)
  const explained = await api.explain(p04.run_id, firstRow.step, firstRow.satellite_id)
  assert.deepEqual(explained.requested, firstRow.requested)
  assert.equal(explained.executed, firstRow.executed)
  assert.equal(explained.consequences.energy_after_wh, firstRow.energy_after_wh)
  assert.equal(typeof explained.score, 'number')
  assert.equal(typeof explained.considered.shadow_prices.energy_wh, 'number')
  assert.ok(Array.isArray(explained.considered.top_candidates))
  // Два уровня ответа. Мелкий ответ обязан честно сказать, что глубокой
  // части нет; full=true обязан её восстановить.
  assert.equal(typeof explained.detail_available, 'boolean')
  if (explained.detail_available) {
    assert.ok(Array.isArray(explained.considered.rejected))
  } else {
    assert.equal(explained.considered.rejected, undefined)
    const deep = await api.explain(p04.run_id, firstRow.step, firstRow.satellite_id, true)
    assert.equal(deep.detail_available, true)
    assert.ok(Array.isArray(deep.considered.rejected))
    assert.equal(deep.considered.recomputed, true)
    assert.deepEqual(deep.requested, firstRow.requested)
  }

  const p01 = await api.createRun({ scenario_id: 'P01_intro', mode: 'centralized', planner: 'economic', label: 'QA потока: событие на границе' })
  owned.push(p01.run_id)
  const eventId = `QA-ADD-${Date.now()}`
  await api.sendEvent(p01.run_id, { id: eventId, type: 'add_jobs', at_step: 0, jobs: [{ id: eventId, kind: 'relay', release_step: 0, deadline_step: 4, work_steps: 1, priority: 3, value_usd: 0, eligible_satellites: ['S01'] }] })
  const boundary = await api.exportRun(p01.run_id, true)
  const reconstructed = reconstructFlow(boundary)
  assert.equal(reconstructed.rows.length, p01.summary.jobs_total + 1)
  assert.equal(reconstructed.rows.find(job => job.job_id === eventId)?.status, 'waiting')
  assert.equal(reconstructed.rows.find(job => job.job_id === eventId)?.remaining_steps, 1)
  await api.advance(p01.run_id, { to_step: 4 })
  const [left, right] = await Promise.all([loadFlow(p04.run_id), loadFlow(p01.run_id)])
  assert.equal(left.rows.length, 8120)
  assert.equal(left.rows.some(job => job.job_id === eventId), false)
  assert.equal(right.rows.filter(job => job.job_id === eventId).length, 1)
  assert.equal(right.step, 4)
  assert.equal(left.step, 288)
  const cost = await api.jobCost('P01_intro', 'JOB-0001')
  assert.ok(cost.cost_cards.every(card => typeof card.marginal_energy_wh === 'number' && typeof card.full_load_energy_wh === 'number'))
  await assert.rejects(() => api.jobCost('P01_intro', eventId))
  console.log(JSON.stringify({ result: 'PASS', p04_jobs: flow.rows.length, counts: flow.counts, capped_completed_rows: reference.jobs.completed.length, event_boundary_reconstruction: true, incomplete_trace_rejected: true, concurrent_runs_isolated: true, economic_explanation_verified: true, marginal_cost_fields_verified: true, event_cost_missing_confirmed: true, requests: calls.length }, null, 2))
} finally {
  for (const id of owned) {
    const response = await fetch(`/api/runs/${id}`, { method: 'DELETE' })
    if (!response.ok) console.error(`Не удалось удалить QA-прогон ${id}: ${response.status}`)
  }
}
