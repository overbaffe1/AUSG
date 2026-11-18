import assert from 'node:assert/strict'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
const origin=process.env.COSMO_TEST_API??'http://127.0.0.1:5018'
const results=[]
async function request(path,body){const response=await fetch(origin+'/api'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Object.assign(new Error(data.error?.message??JSON.stringify(data)),{status:response.status,data});return data}
const record=(name,detail)=>{results.push({name,detail,passed:true});console.log('PASS',name,JSON.stringify(detail))}
const pre=await request('/scenarios/P04_demand/preflight');assert.equal(pre.jobs_total,8120);assert.equal(pre.capacity.downlink.capacity_steps,439);assert.equal(pre.capacity.relay.capacity_steps,13728);assert.equal(pre.energy_short_satellites.length,34);record('P04 preflight',pre.capacity)
const initial=JSON.parse(await readFile(new URL('../../data/P04_demand.json',import.meta.url),'utf8'))
const derived=await request('/scenarios/P04_demand/derive',{initial_soc_pct:{[initial.satellites[0].id]:75},job_priority:{[initial.jobs[0].id]:3},solar_factor:0.9,title:'QA frontend derive'});assert.notEqual(derived.id,'P04_demand');record('derive preserves original scenario',{id:derived.id,original:pre.scenario_id})
const run=await request('/runs',{scenario_id:'P04_demand',planner:'economic',label:'UI acceptance · P04'});record('create P04',{run_id:run.run_id,step:run.step})
const dumped=await request('/runs/'+run.run_id+'/export?include_trace=false');assert.equal(dumped.initial_scenario.jobs.length,8120);assert.equal(dumped.trace,undefined)
const badBefore=await request('/runs/'+run.run_id);await assert.rejects(()=>request('/runs/'+run.run_id+'/events',{id:'bad',at_step:0,type:'satellite_outage',satellite_ids:['MISSING'],end_step:5}));const badAfter=await request('/runs/'+run.run_id);assert.equal(badBefore.state_digest,badAfter.state_digest);record('rejected event keeps state',{step:badAfter.step})
const event={id:'UI-QA-duplicate',at_step:0,type:'satellite_outage',satellite_ids:[initial.satellites[0].id],end_step:2};const accepted=await request('/runs/'+run.run_id+'/events',event);assert.equal(accepted.accepted.length,1);await assert.rejects(()=>request('/runs/'+run.run_id+'/events',event));assert.equal((await request('/runs/'+run.run_id)).state_digest,accepted.state.state_digest);record('duplicate rejection keeps accepted state',{event:event.id})
const changed=await request('/runs/'+run.run_id+'/objective',{objective:'commercial'});assert.equal(changed.history[0].step,0);assert.equal(changed.objective,'commercial');record('objective changes at boundary',changed)
const advance=await request('/runs/'+run.run_id+'/advance',{steps:1});assert.equal(advance.state.step,1);assert.equal(advance.executed_rows,48);record('calculation continues after errors',{step:1,rows:48})
const analysis=await request('/runs/'+run.run_id+'/analytics');assert.equal(analysis.priority_service.share,null);record('P3 absent denominator remains null',analysis.priority_service)
const replay=await request('/runs/import',await request('/runs/'+run.run_id+'/export?include_trace=false'));assert.equal(replay.reproduced,true);record('compact JSON replay',replay.message)
await assert.rejects(()=>request('/scenarios/upload',{invalid:true}));record('invalid scenario rejected',{})
await mkdir(new URL('../.qa/',import.meta.url),{recursive:true});await writeFile(new URL('../.qa/api-smoke-results.json',import.meta.url),JSON.stringify({date:new Date().toISOString(),origin,results},null,2));
