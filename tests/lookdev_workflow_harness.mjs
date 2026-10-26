// Test harness for workflows/lookdev.js (v2). Called by tests/test_lookdev_workflow.py.
//
// LABELLED STUBS: agent(), parallel(), pipeline(), phase(), log() and budget below are test doubles
// of the Claude Code Workflow runtime. No real agent, render or preflight runs here; real runs of
// the workflow happen outside this harness. The stub agent simulates builders, preflight, critic
// and the on-disk receipts; the stub cache simulates Workflow resume as observed in a real run's
// journal.jsonl: a call is served from cache when its request key completed in the earlier run,
// until the first call that misses; every call after that runs live.
//
// Usage: node lookdev_workflow_harness.mjs <scenario> [workflow.js]
// Prints one JSON object on stdout. Exit 0 = scenario ran (assertions live in pytest), 2 = harness error.
// The v1_repro scenario reads the v1 workflow from $SHORTS_STUDIO_DIR/.claude/workflows/shorts-lookdev.js.

import { readFileSync, realpathSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = realpathSync(resolve(HERE, '..'))
const STUDIO_ROOT = '/sim/studio' // simulated: the stub never reads it, prompts only name it
const ROOTS = { pluginRoot: PLUGIN_ROOT, studioRoot: STUDIO_ROOT }
const V2 = resolve(PLUGIN_ROOT, 'workflows/lookdev.js')
function v1Path() {
  const studio = process.env.SHORTS_STUDIO_DIR
  if (!studio) throw new Error('SHORTS_STUDIO_DIR is not set: v1_repro reads <studio>/.claude/workflows/shorts-lookdev.js')
  return resolve(studio, '.claude/workflows/shorts-lookdev.js')
}
const AsyncFunction = (async () => {}).constructor
const RULE_LINES = [
  'No deletions of any kind, including your own scratch copies: write to a new uniquely named path instead.',
  'No full tree copies (cp -R, rsync) into scratch; copy only the named files you need.',
  'Never silence stderr.',
]

// ---- runtime shims: the Workflow runtime throws on these, so the harness does too ----------
const RealDate = Date
function BannedDate(...a) {
  if (a.length === 0) throw new Error('banned: argless new Date()')
  return new RealDate(...a)
}
BannedDate.now = () => { throw new Error('banned: Date.now()') }
const BannedMath = Object.create(Math)
BannedMath.random = () => { throw new Error('banned: Math.random()') }

function splitMeta(src) {
  const m = src.match(/^export const meta = (\{[\s\S]*?\n\})\n/)
  if (!m) throw new Error('script does not start with `export const meta = {...}`')
  return { metaText: m[1], body: src.replace(/^export const meta = /, 'const meta = ') }
}

// A pure literal: after removing string literals only braces, brackets, colons, commas,
// whitespace, keys (identifier followed by ':'), numbers, true/false/null remain.
function metaIsPureLiteral(metaText) {
  const noStrings = metaText
    .replace(/'(?:[^'\\\n]|\\.)*'/g, '""')
    .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')
    .replace(/`[^`$]*`/g, '""')
  if (/[`$()+*/=<>?!&|]/.test(noStrings)) return false
  const idents = noStrings.match(/[A-Za-z_][A-Za-z0-9_]*\s*:?/g) || []
  return idents.every((t) => t.trim().endsWith(':') || ['true', 'false', 'null'].includes(t.trim()))
}

function compile(src) {
  const { body } = splitMeta(src)
  return new AsyncFunction('agent', 'pipeline', 'parallel', 'phase', 'log', 'args', 'budget', 'workflow', 'Date', 'Math', body)
}

// ---- simulated world -------------------------------------------------------------------------
const TOKENS = { plan: 900, build: 1000, segment: 600, assemble: 50, gate: 100, fix: 700, critic: 500, revise: 800, prep: 40 }
const RECEIPT_TOKENS = 20

function keyOf(prompt, opts) {
  return 'v2:' + createHash('sha256').update(JSON.stringify([prompt, opts.model || null, opts.effort || null, opts.schema || null, opts.label || null])).digest('hex')
}

function makeWorld(sc) {
  return {
    sc,
    disk: new Map(), // receipt path -> result (persists across runs of one scenario)
    receiptsEnabled: sc.receipts !== false,
    scores: Object.fromEntries(Object.entries(sc.scores || {}).map(([k, v]) => [k, [...v]])),
    clock: 1792000000,
  }
}

function roleOf(label) { return label.split(':')[0] }
function styleOf(label) { return (label.split(':')[1] || '').split('#')[0] }

function simulate(world, label, prompt) {
  const role = roleOf(label)
  const sc = world.sc
  const t0 = world.clock
  world.clock += 10
  const t1 = world.clock
  const base = { t0, t1, resumed: false }
  if (role === 'prep') return { wav: '/sim/clip.wav', words: '/sim/clip.words.json', transcript: 'sim transcript.', durationSec: 15, lastWordEnd: 14.2 }
  if (role === 'build' && sc.v1) return { mp4: `/sim/${label}.mp4`, stillsDir: `/sim/${label}/stills`, notes: 'n', rationale: 'r', weaknesses: [] }
  if (role === 'revise' && sc.v1) return { mp4: `/sim/${label}.mp4`, stillsDir: `/sim/${label}/stills`, notes: 'n', rationale: 'r', weaknesses: [] }
  if (role === 'critic' && sc.v1) return { score: 5, mustFix: ['busy_frame 1-2 s: x'], keep: ['k'] }
  if (['build', 'fix', 'revise', 'assemble'].includes(role)) {
    if ((sc.delivFail || []).includes(label)) return { ok: false, error: 'simulated build failure', mp4: '', projectDirs: [], voice: '', sfx: '', notes: '', ...base }
    const dirs = role === 'assemble' ? (sc.plan || defaultPlan()).segments.map((s) => `/sim/seg${s.index}/project`) : [`/sim/${label}/project`]
    return { ok: true, mp4: `/sim/${label}.mp4`, projectDirs: dirs, segments: [], voice: role === 'assemble' ? '' : `/sim/${label}/voice_norm.wav`, sfx: role === 'assemble' ? '' : `/sim/${label}/sfx_bus.wav`, contentJson: '', notes: `did ${label}`, weaknesses: [], ...base }
  }
  if (role === 'segment') {
    const idx = Number(label.split('.s')[1])
    return { ok: true, index: idx, mp4: `/sim/seg${idx}.mp4`, projectDir: `/sim/seg${idx}/project`, durationSec: 20, notes: '', ...base }
  }
  if (role === 'gate') {
    if (sc.gateRaw && sc.gateRaw[label]) return { ...sc.gateRaw[label], ...base }
    const pass = !(sc.gateFail || []).includes(label)
    const checks = ['dead_frames', 'endcard', 'loudness', 'legibility'].map((name) => ({
      name, passed: name === 'dead_frames' ? pass : true, kinds: name === 'dead_frames' && !pass ? ['black'] : [], detail: name === 'dead_frames' && !pass ? 'black 3.0-4.0 s' : '',
    }))
    return { ok: true, passed: pass, reportPath: `/sim/${label}/preflight_report.json`, sheetsJson: `/sim/${label}/sheets.json`, checks, ...base }
  }
  if (role === 'critic') {
    const q = world.scores[styleOf(label)]
    if (!q || !q.length) throw new Error(`scenario has no critic score left for ${label}`)
    return { score: q.shift(), mustFix: [`busy_frame 1.0-2.0 s: ${label}`], keep: ['palette'], ...base }
  }
  if (role === 'plan') return { ok: true, ...(sc.plan || defaultPlan()), planPath: '/sim/plan.json', ...base }
  throw new Error(`no simulation for ${label}`)
}

function defaultPlan() {
  return {
    durationSec: 60, lastWordEnd: 59.5, transcript: 'full transcript.',
    segments: [
      { index: 0, t_start: 0, t_end: 20, summary: 'a', visual: 'A' },
      { index: 1, t_start: 20, t_end: 40, summary: 'b', visual: 'B' },
      { index: 2, t_start: 40, t_end: 60, summary: 'c', visual: 'C' },
    ],
  }
}

// One execution of a workflow script against the world. cache = Map(key -> result) from an
// earlier run (resume) or null.
async function execute(src, args, world, { cache = null, hang = [], delays = {} } = {}) {
  const run = { calls: [], logs: [], works: [], receiptHits: [], cached: [], completed: new Map(), spent: 0, broken: false, pending: 0, hangIssued: false, phases: [] }
  const agent = (prompt, opts = {}) => {
    const label = opts.label || '?'
    const key = keyOf(prompt, opts)
    run.calls.push({ label, key, model: opts.model ?? null, effort: opts.effort ?? null, phase: opts.phase ?? null, hasSchema: !!opts.schema, agentType: opts.agentType ?? null, prompt })
    if (cache) {
      if (!run.broken && cache.has(key)) {
        run.cached.push(label)
        run.completed.set(key, cache.get(key))
        return Promise.resolve(cache.get(key))
      }
      run.broken = true
    }
    if (hang.includes(label)) {
      run.hangIssued = true
      return new Promise(() => {})
    }
    run.pending += 1
    return new Promise((res, rej) => {
      setTimeout(() => {
        run.pending -= 1
        try {
          const m = prompt.match(/^RESUME RECEIPT: (\S+)$/m)
          let result
          if (m && world.receiptsEnabled && world.disk.has(m[1])) {
            run.receiptHits.push(label)
            result = { ...world.disk.get(m[1]), resumed: true }
            run.spent += RECEIPT_TOKENS
          } else {
            run.works.push(label)
            result = simulate(world, label, prompt)
            if (m) world.disk.set(m[1], result)
            run.spent += TOKENS[roleOf(label)] || 1
          }
          run.completed.set(key, result)
          res(result)
        } catch (e) { rej(e) }
      }, delays[label] ?? 1)
    })
  }
  const parallel = (thunks) => Promise.all(thunks.map((t) => {
    try { return Promise.resolve(t()).catch(() => null) } catch { return null }
  }))
  const pipeline = (items, ...stages) => Promise.all(items.map((item, i) =>
    stages.reduce((p, stage) => p.then((prev) => stage(prev, item, i)), Promise.resolve(item)).catch(() => null)))
  const phase = (t) => { run.phases.push(t) }
  const log = (m) => { run.logs.push(String(m)) }
  const budget = { spent: () => run.spent }
  const workflow = () => { throw new Error('workflow() not used by lookdev') }
  const fn = compile(src)
  let settled = false
  const done = fn(agent, pipeline, parallel, phase, log, args, budget, workflow, BannedDate, BannedMath)
    .then((r) => ({ finished: true, result: r }), (e) => ({ finished: true, error: String(e && e.message || e) }))
    .finally(() => { settled = true })
  // "kill": once a hanging call is in flight and every other live call has resolved
  const killed = new Promise((res) => {
    const tick = () => {
      if (settled) return
      if (run.hangIssued && run.pending === 0) setTimeout(() => res({ finished: false }), 20)
      else setTimeout(tick, 5)
    }
    tick()
  })
  const out = await Promise.race([done, killed])
  return { ...out, run }
}

function summary(x) {
  const r = x.run
  return {
    finished: x.finished, error: x.error || null, result: x.result || null,
    labels: r.calls.map((c) => c.label), keys: r.calls.map((c) => c.key),
    works: r.works, receiptHits: r.receiptHits, cached: r.cached, logs: r.logs, phases: r.phases,
    calls: r.calls.map((c) => ({ label: c.label, model: c.model, effort: c.effort, phase: c.phase, hasSchema: c.hasSchema, prompt: c.prompt })),
  }
}

// ---- scenarios -----------------------------------------------------------------------------
const CUT = { status: 'OK', cut: 15.1, wav: '/sim/in/S49-cut15.wav', words: '/sim/in/S49-cut15.words.json', txt: '/sim/in/S49-cut15.txt', transcript: 'Two students who studied from the same notes.', durationSec: 15.1, lastWordEnd: 14.55 }
const lookArgs = (styles, extra = {}) => ({ mode: 'look', styles, runTag: 'sim', cut: CUT, outRoot: '/sim/out', brand: '/sim/brand.json', ports: { base: 3300 }, ...ROOTS, ...extra })

async function single(src, args, sc, opts) {
  const world = makeWorld(sc)
  return summary(await execute(src, args, world, opts))
}

// run 1 is killed at the hang labels; run 2 resumes with run 1's completed results as the cache
async function resume(src, args, sc, { hang, delays1 = {}, delays2 = {}, receipts2 = true }) {
  const world = makeWorld(sc)
  const r1 = await execute(src, args, world, { hang, delays: delays1 })
  const world2 = { ...world, receiptsEnabled: receipts2 }
  world2.scores = world.scores // same remaining queue: a critic that never ran is still owed
  const r2 = await execute(src, args, world2, { cache: r1.run.completed, delays: delays2 })
  return { run1: summary(r1), run2: summary(r2), completedInRun1: r1.run.works }
}

const SCENARIOS = {
  meta: async (src) => {
    const { metaText } = splitMeta(src)
    const meta = runInNewContext(`(${metaText})`, Object.create(null), { timeout: 1000 })
    return { pure: metaIsPureLiteral(metaText), meta }
  },
  meta_impure: async () => {
    // planted negative: a meta that calls a function
    const bad = "{\n  name: 'x',\n  description: String(1),\n  phases: [],\n}"
    return { pure: metaIsPureLiteral(bad) }
  },
  ship_after_2: (src) => single(src, lookArgs(['D']), { scores: { D: [5, 6, 8] } }),
  stop_at_2: (src) => single(src, lookArgs(['D']), { scores: { D: [5, 6, 6] } }),
  ship_first: (src) => single(src, lookArgs(['D']), { scores: { D: [7] } }),
  max_revise_0: (src) => single(src, lookArgs(['D'], { maxRevise: 0 }), { scores: { D: [4] } }),
  gate_fail_fix: (src) => single(src, lookArgs(['D']), { scores: { D: [8] }, gateFail: ['gate:D#a0'] }),
  gate_fail_cap: (src) => single(src, lookArgs(['D']), { scores: { D: [8, 8] }, gateFail: ['gate:D#a0', 'gate:D#a1'] }),
  gate_lies: (src) => single(src, lookArgs(['D']), {
    scores: { D: [8] },
    gateRaw: { 'gate:D#a0': { ok: true, passed: true, reportPath: '/sim/r.json', sheetsJson: '/sim/s.json', checks: ['dead_frames', 'endcard', 'loudness'].map((name) => ({ name, passed: true, kinds: [], detail: '' })) } },
  }),
  gate_runner_error: (src) => single(src, lookArgs(['D']), { scores: { D: [8] }, gateRaw: { 'gate:D#a0': { ok: false, error: 'exit 2: no such file', passed: false, reportPath: '', sheetsJson: '', checks: [] } } }),
  build_fails: (src) => single(src, lookArgs(['D']), { scores: { D: [8] }, delivFail: ['build:D#a0'] }),
  prep_missing_cut: (src) => single(src, { mode: 'look', styles: ['D'], runTag: 'sim', outRoot: '/sim/out', ...ROOTS }, {}),
  prep_out_of_window: (src) => single(src, lookArgs(['D'], { cut: { ...CUT, durationSec: 20.4, cut: 20.4 } }), {}),
  prep_cut_fail_status: (src) => single(src, lookArgs(['D'], { cut: { status: 'FAIL', reason: 'FAIL: no sentence-end silence gap gives a cut in 12.5-17.5 s' } }), {}),
  prep_bad_style: (src) => single(src, lookArgs(['Z']), {}),
  prep_outroot_in_plugin: (src) => single(src, lookArgs(['D'], { outRoot: `${PLUGIN_ROOT}/renders` }), {}),
  multi_sequential: (src) => single(src, lookArgs(['D', 'E', 'K']), { scores: { D: [7], E: [5, 7], K: [6, 6, 6] } }),
  multi_parallel: (src) => single(src, lookArgs(['D', 'E', 'K'], { parallel: true }), { scores: { D: [7], E: [5, 7], K: [6, 6, 6] } }),
  order_invariance: async (src) => {
    const sc = { scores: { D: [5, 7], E: [5, 7], K: [5, 7] } }
    const a = await single(src, lookArgs(['D', 'E', 'K'], { parallel: true }), sc, { delays: { 'build:D#a0': 40, 'build:E#a0': 20, 'build:K#a0': 1, 'revise:D#a1': 1, 'revise:K#a1': 30 } })
    const b = await single(src, lookArgs(['D', 'E', 'K'], { parallel: true }), sc, { delays: { 'build:D#a0': 1, 'build:E#a0': 20, 'build:K#a0': 40, 'revise:D#a1': 30, 'revise:K#a1': 1 } })
    return { a: a.keys, b: b.keys, labelsA: a.labels, labelsB: b.labels }
  },
  deterministic_prompts: async (src) => {
    const sc = { scores: { D: [5, 8] }, gateFail: ['gate:D#a0'] }
    const a = await single(src, lookArgs(['D']), sc)
    const b = await single(src, lookArgs(['D']), sc)
    return { same: JSON.stringify(a.calls.map((c) => c.prompt)) === JSON.stringify(b.calls.map((c) => c.prompt)), n: a.calls.length }
  },
  resume_sequential: (src) => resume(src, lookArgs(['D']), { scores: { D: [5, 6, 8] } }, { hang: ['revise:D#a2'] }),
  resume_parallel: (src) => resume(src, lookArgs(['D', 'E', 'K'], { parallel: true }), { scores: { D: [5, 8], E: [5, 8], K: [5, 8] } }, {
    hang: ['revise:D#a1'], delays1: { 'build:K#a0': 1, 'build:E#a0': 15, 'build:D#a0': 30 },
  }),
  resume_parallel_no_receipts: (src) => resume(src, lookArgs(['D', 'E', 'K'], { parallel: true }), { scores: { D: [5, 8], E: [5, 8], K: [5, 8] } }, {
    hang: ['revise:D#a1'], receipts2: false,
  }),
  // planted reproduction of an observed v1 resume failure: v1 (pipeline) with R running ahead, killed while C, D, E revise
  v1_repro: async () => {
    const src = readFileSync(v1Path(), 'utf8')
    const args = { source: { wav: '/sim/src.wav', words: '/sim/src.words.json' }, targetSec: 15, runTag: 'sim', portBase: 3080, directions: ['C', 'D', 'E', 'R'].map((key) => ({ key, name: key, brief: 'b' })) }
    const delays1 = { 'build:C': 30, 'build:D': 40, 'build:E': 20, 'build:R': 1, 'critic:R': 1, 'revise:R': 1 }
    return resume(src, args, { v1: true }, { hang: ['revise:C', 'revise:D', 'revise:E'], delays1 })
  },
  full_mode: (src) => single(src, { mode: 'full', styles: ['D'], runTag: 'simfull', wav: '/sim/S49-full.wav', words: '/sim/S49.words.json', outRoot: '/sim/out', ...ROOTS }, { scores: { D: [6, 7] } }),
  full_bad_plan: (src) => single(src, { mode: 'full', styles: ['D'], runTag: 'simfull', wav: '/sim/S49-full.wav', words: '/sim/S49.words.json', outRoot: '/sim/out', ...ROOTS }, {
    scores: { D: [7] },
    plan: { durationSec: 60, lastWordEnd: 59, transcript: 't', segments: [{ index: 0, t_start: 0, t_end: 20, summary: 'a', visual: 'A' }, { index: 1, t_start: 25, t_end: 60, summary: 'b', visual: 'B' }] },
  }),
  banned_api: async (src) => {
    // planted negative: the same script with a Date.now() call must throw under the runtime shims
    const bad = src.replace("const tokensAtStart = spent()", "const tokensAtStart = spent() + Date.now() * 0")
    if (bad === src) throw new Error('banned_api: anchor not found')
    return single(bad, lookArgs(['D']), { scores: { D: [7] } })
  },
  rules_mutated: async (src) => {
    // planted negative: one hard-rule line altered; the policy check in pytest must flag it
    const bad = src.replace("  'Never silence stderr.',\n", "  'Avoid silencing stderr.',\n")
    if (bad === src) throw new Error('rules_mutated: anchor not found')
    return single(bad, lookArgs(['D']), { scores: { D: [7] } })
  },
}

const name = process.argv[2]
const path = process.argv[3] || V2
try {
  if (!SCENARIOS[name]) throw new Error(`unknown scenario ${name}; known: ${Object.keys(SCENARIOS).join(', ')}`)
  const out = await SCENARIOS[name](readFileSync(path, 'utf8'))
  process.stdout.write(JSON.stringify({ scenario: name, ruleLines: RULE_LINES, ...out }) + '\n')
} catch (e) {
  process.stderr.write(`harness error: ${e && e.stack || e}\n`)
  process.exit(2)
}
