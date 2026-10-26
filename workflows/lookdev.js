export const meta = {
  name: 'shorts-lookdev-v2',
  description: 'Look-dev v2: build shorts from the plugin style templates, gate them with the preflight checks, score them with the rubric critic, and revise while the score is below 7 (at most 2 revisions). Modes: look (15 s cut) and full (one scene plan, then per-segment builders).',
  whenToUse: 'Called by the shorts-studio skill after it has run pick_cut.py itself. Args: {mode, styles, cut, wav, words, transcript, brand, ports, runTag, outRoot, parallel, maxRevise, maxGateFixes, quality, maxSegments, pluginRoot, studioRoot}. See the ARGS comment block in workflows/lookdev.js.',
  phases: [
    { title: 'Prep', detail: 'validate the pick_cut.py result passed in args (no agent)' },
    { title: 'Plan', detail: 'full mode only: one scene plan on opus at high effort' },
    { title: 'Build', detail: 'template build and render per style (per segment in full mode)' },
    { title: 'Preflight', detail: 'evidence.py report running the dead_frames, endcard, loudness and legibility gates; a failure returns to the builder' },
    { title: 'Critique', detail: 'rubric critic on opus at medium effort over contact sheets and the preflight report' },
    { title: 'Revise', detail: 'revise on opus at medium effort, then preflight and re-score; loop while score < 7, at most 2 times' },
  ],
}

// ---------------------------------------------------------------------------------------------
// ARGS (validated below; a bad value returns {ok:false, fail} before any agent runs)
//   mode          'look' | 'full'                                              required
//   styles        'D' or ['D','E','K']: style keys A-T (K builds variant K16)  required
//   runTag        [A-Za-z0-9._-]+, names the run dirs                          required
//   cut           look: the JSON line pick_cut.py printed, with its --out-* paths:
//                 {status:'OK', cut, wav, words, transcript, durationSec, lastWordEnd}. The CALLER
//                 runs pick_cut.py (workflow scripts cannot run processes). durationSec must lie
//                 in 15 +- 2.5 s. full: must be absent or null.
//   wav, words    look: optional, must equal cut.wav / cut.words. full: required (whole narration).
//   transcript    full: optional text (the planner reads the words file either way).
//   brand         optional absolute path to a brand JSON passed to build.py --brand.
//   ports         optional preview ports: a number (base), {base: n}, or {<key>: n}. Default base 3180.
//   outRoot       required absolute dir outside pluginRoot, e.g. ${CLAUDE_PLUGIN_DATA}/lookdev-v2.
//                 Each style writes only under <outRoot>/<runTag>-<key>/a<N>/ (one dir per attempt).
//   parallel      optional boolean, default false. false: styles run one after another and every
//                 step is a single agent call (exact per-step tokens, one render at a time).
//                 true: lockstep rounds, one step per active style per round via parallel().
//   maxRevise     optional int 0-2, default 2.   maxGateFixes  optional int 0-2, default 1.
//   quality       optional hyperframes -q value: draft | looks | delivery | standard | high (default looks).
//   maxSegments   full: optional int 2-16, default 10.
//   pluginRoot    required absolute path of this plugin: pass ${CLAUDE_PLUGIN_ROOT}.
//   studioRoot    required absolute path of the studio dir: pass the value of SHORTS_STUDIO_DIR.
//                 The Workflow runtime gives the script no environment and no script path, so the
//                 caller resolves both and passes them in; there are no built-in defaults.
//                 The sandbox profile is the vendored <pluginRoot>/kit/offline.sb. The reference
//                 reel is read from <studioRoot>/_kit/lookdev/inputs/references/ when it exists.
//
// RETURN {ok, version:2, mode, runTag, runDir, clip, plan, results:[...], journal:[...], usage, fail?}
//   results[i] = {key, status:'shipped'|'below_bar'|'error', score, rawScore, gatePassed, mp4,
//                 attemptDir, attempts, revisions, gateFixes, scores:[{attempt, raw, score, gatePassed}],
//                 mustFix, keep, error}
//   journal[i] = {seq, round, style, step, attempt, label, model, effort, t0, t1, wallS, tokens,
//                 tokensMode:'exact'|'pooled', pooledCalls, cachedLikely, resumed, outcome}
//
// RESUME. The Workflow tool keys each agent() call by a hash of its request and, on resume,
// serves cached results only until the first call that misses; every later call runs live.
// Journal evidence, run wf_01693fb8-20f (v1, pipeline()): revise:R completed in the first run
// (key v2:37fbd99...) but was re-started on resume, because replay issued the calls in style
// order (revise:C, the first miss, came before revise:R) while the original run issued them in
// completion order. v2 therefore (1) never uses pipeline(): calls are issued in a fixed order,
// one at a time (parallel:false) or in lockstep rounds in style order (parallel:true), so a
// completed step is always inside the replayed prefix; (2) keeps every prompt a pure function
// of args and earlier results (no timestamps, no budget numbers); and (3) for the one gap left,
// the steps issued after a miss inside an interrupted parallel round, every agent first checks
// an on-disk receipt <styleDir>/receipts/<stepId>.<stepKey>.json (stepKey = hash of the step
// prompt) and returns it instead of redoing the work. Receipts are reused whenever the same
// step prompt recurs under the same runTag, so start a fresh run with a new runTag.
// ---------------------------------------------------------------------------------------------

const LOOK_TARGET = 15
const LOOK_TOL = 2.5
const SHIP_SCORE = 7
const GATE_CAP = 6
const MAX_REVISE = 2
const MAX_GATE_FIXES = 2
const REQUIRED_GATES = ['dead_frames', 'endcard', 'loudness', 'legibility']
const STYLE_KEYS = 'ABCDEFGHIJKLMNOPQRST'.split('')
const QUALITIES = ['draft', 'looks', 'delivery', 'standard', 'high']

// One place for the model mix. Every agent() call takes model and effort from here.
const ROLES = {
  plan: { model: 'opus', effort: 'high', phase: 'Plan' },
  build: { model: 'sonnet', effort: 'medium', phase: 'Build' },
  segment: { model: 'sonnet', effort: 'medium', phase: 'Build' },
  assemble: { model: 'sonnet', effort: 'low', phase: 'Build' },
  gate: { model: 'sonnet', effort: 'low', phase: 'Preflight' },
  fix: { model: 'opus', effort: 'medium', phase: 'Preflight' },
  critic: { model: 'opus', effort: 'medium', phase: 'Critique' },
  revise: { model: 'opus', effort: 'medium', phase: 'Revise' },
}
const OVERRIDE = {
  plan: 'EFFORT_CEILING_OVERRIDE — reason: the scene plan fixes segment boundaries and visual intent for every segment build of a full-length video.',
}

const HARD_RULES = [
  'No deletions of any kind, including your own scratch copies: write to a new uniquely named path instead.',
  'No full tree copies (cp -R, rsync) into scratch; copy only the named files you need.',
  'Never silence stderr.',
].join('\n')

const TIMING = 'TIMING: run `date +%s` as your first command and report it as t0; run it again just before you answer and report it as t1 (integer epoch seconds).'

// ---------------------------------------------------------------------------------------------
// schemas
const T = { type: 'number' }
const S = { type: 'string' }
const SA = { type: 'array', items: S }
const DELIV = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' }, error: S, mp4: S, projectDirs: SA, segments: SA, voice: S, sfx: S, contentJson: S,
    notes: S, weaknesses: SA, t0: T, t1: T, resumed: { type: 'boolean' },
  },
  required: ['ok', 'mp4', 'projectDirs', 'voice', 'sfx', 'notes', 't0', 't1', 'resumed'],
}
const SEG = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' }, error: S, index: T, mp4: S, projectDir: S, durationSec: T, notes: S,
    t0: T, t1: T, resumed: { type: 'boolean' },
  },
  required: ['ok', 'index', 'mp4', 'projectDir', 'durationSec', 't0', 't1', 'resumed'],
}
const GATE = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' }, error: S, passed: { type: 'boolean' }, reportPath: S, sheetsJson: S,
    checks: {
      type: 'array',
      items: {
        type: 'object',
        properties: { name: S, passed: { type: 'boolean' }, kinds: SA, detail: S },
        required: ['name', 'passed', 'kinds', 'detail'],
      },
    },
    t0: T, t1: T, resumed: { type: 'boolean' },
  },
  required: ['ok', 'passed', 'reportPath', 'sheetsJson', 'checks', 't0', 't1', 'resumed'],
}
const CRIT = {
  type: 'object',
  properties: {
    score: { type: 'number', description: '1-10 on the rubric anchors (5 usable with fixes, 7 shippable, 9 matches the reel)' },
    mustFix: SA, keep: SA, t0: T, t1: T, resumed: { type: 'boolean' },
  },
  required: ['score', 'mustFix', 'keep', 't0', 't1', 'resumed'],
}
const PLAN = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' }, error: S, durationSec: T, lastWordEnd: T, transcript: S, planPath: S,
    segments: {
      type: 'array',
      items: {
        type: 'object',
        properties: { index: T, t_start: T, t_end: T, summary: S, visual: S },
        required: ['index', 't_start', 't_end', 'summary', 'visual'],
      },
    },
    t0: T, t1: T, resumed: { type: 'boolean' },
  },
  required: ['ok', 'durationSec', 'lastWordEnd', 'transcript', 'segments', 'planPath', 't0', 't1', 'resumed'],
}

// ---------------------------------------------------------------------------------------------
// pure helpers

// FNV-1a, two lanes, 16 hex chars. Deterministic; used only to name receipts.
function stepKey(text) {
  let a = 0x811c9dc5
  let b = 0x9747b28c
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i)
    a = Math.imul(a ^ c, 0x01000193) >>> 0
    b = Math.imul(b ^ c, 0x5bd1e995) >>> 0 // odd multiplier, or the low bits collapse
  }
  return a.toString(16).padStart(8, '0') + b.toString(16).padStart(8, '0')
}

function shq(s) {
  return `'${String(s).split("'").join(`'\\''`)}'`
}

function isAbs(p) {
  return typeof p === 'string' && p.startsWith('/') && !p.includes('\n')
}

function intIn(v, lo, hi, dflt, name) {
  if (v === undefined || v === null) return dflt
  if (!Number.isInteger(v) || v < lo || v > hi) throw new Error(`${name} must be an integer ${lo}-${hi}, got ${JSON.stringify(v)}`)
  return v
}

function validateArgs(a) {
  if (!a || typeof a !== 'object') throw new Error('args must be an object')
  const mode = a.mode
  if (mode !== 'look' && mode !== 'full') throw new Error(`mode must be 'look' or 'full', got ${JSON.stringify(mode)}`)
  const raw = typeof a.styles === 'string' ? [a.styles] : a.styles
  if (!Array.isArray(raw) || raw.length === 0) throw new Error('styles must be a style key or a non-empty array of keys (A-T)')
  const styles = raw.map((k) => String(k).trim().toUpperCase())
  for (const k of styles) if (!STYLE_KEYS.includes(k)) throw new Error(`unknown style key ${JSON.stringify(k)} (expected A-T)`)
  if (new Set(styles).size !== styles.length) throw new Error('styles has duplicate keys')
  if (typeof a.runTag !== 'string' || !/^[A-Za-z0-9._-]+$/.test(a.runTag)) throw new Error('runTag is required ([A-Za-z0-9._-]+)')
  const pluginRoot = a.pluginRoot
  if (pluginRoot === undefined || pluginRoot === null || pluginRoot === '') throw new Error('pluginRoot is required: pass the plugin directory (${CLAUDE_PLUGIN_ROOT}) as args.pluginRoot')
  const studioRoot = a.studioRoot
  if (studioRoot === undefined || studioRoot === null || studioRoot === '') throw new Error('studioRoot is required: set SHORTS_STUDIO_DIR to the studio directory and pass its value as args.studioRoot')
  if (!isAbs(pluginRoot) || !isAbs(studioRoot)) throw new Error('pluginRoot and studioRoot must be absolute paths')
  const outRoot = a.outRoot
  if (outRoot === undefined || outRoot === null || outRoot === '') throw new Error('outRoot is required: pass an absolute output dir outside the plugin, e.g. ${CLAUDE_PLUGIN_DATA}/lookdev-v2')
  if (!isAbs(outRoot)) throw new Error('outRoot must be an absolute path')
  if (outRoot === pluginRoot || outRoot.startsWith(`${pluginRoot}/`)) throw new Error('outRoot must be outside pluginRoot')
  if (a.brand !== undefined && a.brand !== null && !isAbs(a.brand)) throw new Error('brand must be an absolute path to a brand JSON')
  const quality = a.quality === undefined ? 'looks' : a.quality
  if (!QUALITIES.includes(quality)) throw new Error(`quality must be one of ${QUALITIES.join(', ')}`)
  if (a.parallel !== undefined && typeof a.parallel !== 'boolean') throw new Error('parallel must be a boolean')

  let clip
  if (mode === 'look') {
    const c = a.cut
    if (!c || typeof c !== 'object') {
      throw new Error('cut is missing: run pick_cut.py first (--target 15 --lo 12.5 --hi 17.5 with --out-wav/--out-words/--out-txt) and pass its JSON line as args.cut')
    }
    if (c.status !== undefined && c.status !== 'OK') throw new Error(`pick_cut.py reported ${c.status}: ${c.reason || 'no reason given'}; nothing built`)
    for (const f of ['wav', 'words']) if (!isAbs(c[f])) throw new Error(`cut.${f} must be the absolute --out-${f} path pick_cut.py wrote`)
    if (typeof c.transcript !== 'string' || !c.transcript.trim()) throw new Error('cut.transcript is missing')
    for (const f of ['durationSec', 'lastWordEnd']) if (typeof c[f] !== 'number' || !Number.isFinite(c[f])) throw new Error(`cut.${f} must be a number`)
    const lo = LOOK_TARGET - LOOK_TOL
    const hi = LOOK_TARGET + LOOK_TOL
    if (c.durationSec < lo || c.durationSec > hi) throw new Error(`cut.durationSec ${c.durationSec} is outside the look window ${lo}-${hi} s`)
    if (c.lastWordEnd <= 0 || c.lastWordEnd > c.durationSec + 0.01) throw new Error(`cut.lastWordEnd ${c.lastWordEnd} is not inside the clip`)
    if (a.wav !== undefined && a.wav !== c.wav) throw new Error('args.wav differs from cut.wav')
    if (a.words !== undefined && a.words !== c.words) throw new Error('args.words differs from cut.words')
    clip = { wav: c.wav, words: c.words, transcript: c.transcript, durationSec: c.durationSec, lastWordEnd: c.lastWordEnd, cut: typeof c.cut === 'number' ? c.cut : c.durationSec }
  } else {
    if (a.cut !== undefined && a.cut !== null) throw new Error('full mode takes the whole narration: pass wav and words, not cut')
    if (!isAbs(a.wav) || !isAbs(a.words)) throw new Error('full mode needs absolute wav and words paths')
    clip = { wav: a.wav, words: a.words, transcript: typeof a.transcript === 'string' ? a.transcript : '' }
  }

  let ports = { base: 3180 }
  if (typeof a.ports === 'number') ports = { base: a.ports }
  else if (a.ports && typeof a.ports === 'object') ports = { ...a.ports }
  return {
    mode, styles, runTag: a.runTag, pluginRoot, studioRoot, outRoot, clip, quality,
    brand: a.brand || null,
    ports,
    parallel: a.parallel === true,
    maxRevise: intIn(a.maxRevise, 0, MAX_REVISE, MAX_REVISE, 'maxRevise'),
    maxGateFixes: intIn(a.maxGateFixes, 0, MAX_GATE_FIXES, 1, 'maxGateFixes'),
    maxSegments: intIn(a.maxSegments, 2, 16, 10, 'maxSegments'),
    runDir: `${outRoot}/${a.runTag}`,
    sandbox: `${pluginRoot}/kit/offline.sb`,
  }
}

function portFor(cfg, key, index, seg) {
  if (typeof cfg.ports[key] === 'number') return cfg.ports[key] + (seg || 0)
  const base = typeof cfg.ports.base === 'number' ? cfg.ports.base : 3180
  return base + index * 20 + (seg || 0)
}

// The gate decision is made here from the data, not from the agent's word.
function evaluateGate(g) {
  if (!g || g.ok !== true || !Array.isArray(g.checks)) {
    return { ran: false, passed: false, missing: [], failed: [], reason: g && g.error ? g.error : 'preflight runner returned no result' }
  }
  const names = new Set(g.checks.map((c) => c.name))
  const missing = REQUIRED_GATES.filter((n) => !names.has(n))
  const failed = g.checks.filter((c) => c.passed !== true).map((c) => c.name)
  const passed = missing.length === 0 && failed.length === 0 && g.passed === true
  const reason = passed ? '' : [
    missing.length ? `missing checks: ${missing.join(', ')}` : '',
    failed.length ? `failed checks: ${failed.join(', ')}` : '',
    g.passed !== true && !failed.length && !missing.length ? 'report.passed is false' : '',
  ].filter(Boolean).join('; ')
  return { ran: true, passed, missing, failed, reason }
}

function capScore(raw, gatePassed) {
  return gatePassed ? raw : Math.min(raw, GATE_CAP)
}

function validScore(s) {
  return typeof s === 'number' && Number.isFinite(s) && s >= 1 && s <= 10
}

function validatePlan(p, cfg) {
  if (!p || p.ok !== true) return (p && p.error) || 'scene plan returned no result'
  const segs = p.segments
  if (!Array.isArray(segs) || segs.length === 0) return 'scene plan has no segments'
  if (segs.length > cfg.maxSegments) return `scene plan has ${segs.length} segments (max ${cfg.maxSegments})`
  if (!(p.durationSec > 0)) return 'scene plan has no durationSec'
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i]
    if (s.index !== i) return `segment ${i} has index ${s.index}`
    if (!(s.t_end > s.t_start)) return `segment ${i} is empty`
    if (s.t_end - s.t_start < 3) return `segment ${i} is shorter than 3 s`
    const prevEnd = i === 0 ? 0 : segs[i - 1].t_end
    if (Math.abs(s.t_start - prevEnd) > 0.001) return `segment ${i} starts at ${s.t_start}, expected ${prevEnd}`
  }
  if (Math.abs(segs[segs.length - 1].t_end - p.durationSec) > 0.05) return 'the last segment does not end at durationSec'
  return null
}

function fmt(n) {
  return Number(n.toFixed(3)).toString()
}

// ---------------------------------------------------------------------------------------------
// prompts (pure functions of cfg and earlier results)

function header(role) {
  const r = ROLES[role]
  return [`Effort: ${r.effort}`, ...(r.effort === 'high' ? [OVERRIDE[role]] : [])].join('\n')
}

function withReceipt(role, styleDir, stepId, body) {
  const key = stepKey(`${role}\n${body}`)
  const receipt = `${styleDir}/receipts/${stepId}.${key}.json`
  return [
    header(role),
    TIMING,
    HARD_RULES,
    `RESUME RECEIPT: ${receipt}`,
    '- Before any other work, test whether that file exists. If it does, Read it and return its "result" object exactly, with resumed set to true. Do nothing else.',
    `- Otherwise do the step below, set resumed to false, and as your last action write {"stepKey": "${key}", "result": <your final answer>} to that exact path as a new file (create the receipts dir if needed; never overwrite or remove another file).`,
    '',
    body,
  ].join('\n')
}

function narration(cfg) {
  const c = cfg.clip
  const dur = typeof c.durationSec === 'number' ? ` (${fmt(c.durationSec)} s, last word ends ${fmt(c.lastWordEnd)} s)` : ''
  return `NARRATION: wav ${c.wav}${dur}; word timings ${c.words} (lookdev format {id,text,start,end}); transcript: "${c.transcript}"`
}

function craft(cfg, st, port) {
  const P = cfg.pluginRoot
  return [
    `PLUGIN (read only): ${P}. Style ${st.key} template: ${P}/styles/${st.key}/ (entry build.py: read \`python3 ${P}/styles/${st.key}/build.py --help\` and its module docstring before running it). Registry entry: the object with "key": "${st.key}" in ${P}/styles/styles.json.`,
    `BRAND: ${cfg.brand ? `pass --brand ${cfg.brand}` : 'none (kit defaults)'}.`,
    `FONTS: only the bundled open-licence fonts in ${P}/kit/fonts (Virgil and Lilita One under OFL 1.1, Comic Shanns under MIT). Never Comic Sans, Marker Felt, Chalkduster, SF Pro or SF Mono.`,
    `RENDER: cd <project dir> && npm_config_offline=true sandbox-exec -f ${cfg.sandbox} npx hyperframes@0.8.183 render . -q ${cfg.quality} -w 2 -o <mp4>. Send stdout and stderr to <mp4>.render.log (never /dev/null) and check the exit code; read the log tail on failure. Never -w auto. Never kill processes. If you start a preview, use port ${port} and stop it with --stop.`,
    `WRITE SCOPE: only under ${st.dir}/. Never edit anything under ${P} or ${cfg.studioRoot}/_kit/lookdev (read or copy single named files only). No network, no downloads.`,
  ].join('\n')
}

const DELIV_RETURN = 'RETURN: ok (false only if you could not produce the mp4; then put the reason in error), mp4, projectDirs (absolute HyperFrames project dirs used), voice and sfx (absolute paths of the normalised voice stem and the SFX bus stem from the build manifest or the project assets, or "" if the build has none), contentJson (path or ""), segments (full mode: every segment mp4 in order, rebuilt or reused; look mode: []), notes (one paragraph: what you did), weaknesses, t0, t1, resumed.'

function buildPrompt(cfg, st, n) {
  const dir = `${st.dir}/a${n}`
  const body = [
    `TASK: build style ${st.key} for run ${cfg.runTag}, attempt a${n}, from the plugin template. You are the builder; preflight gates and an independent critic judge the result after you.`,
    narration(cfg),
    craft(cfg, st, portFor(cfg, st.key, st.index, 0)),
    'STEPS:',
    `1. If build.py accepts --content, write ${dir}/content.json with the template's content keys tailored to this transcript (only claims the narration makes; no invented facts or numbers). Otherwise skip it.`,
    `2. Run python3 ${cfg.pluginRoot}/styles/${st.key}/build.py --wav ${cfg.clip.wav} --words ${cfg.clip.words} --out ${dir}/build${cfg.brand ? ` --brand ${cfg.brand}` : ''} [--content ${dir}/content.json] --json, saving stdout to ${dir}/build.json. Exit 1 means the template's own loudness gate failed: note it and continue. Exit 2: return ok false with the stderr message.`,
    `3. The manifest's "project" value is the HyperFrames project dir (some styles use <out>/project, some use <out> itself). If the template has its own --render flag you may use it instead of RENDER, with -w 2.`,
    `4. Render to ${dir}/look.mp4 and confirm it exists and its ffprobe duration covers the narration plus the end card. Do not judge the look yourself.`,
    DELIV_RETURN,
  ].join('\n')
  return withReceipt('build', st.dir, `a${n}.build`, body)
}

function gateCommand(cfg, deliv, evDir) {
  const P = cfg.pluginRoot
  const words = cfg.clip.words
  const loud = ['loudness {video}']
  if (deliv.voice) loud.push(`--voice ${shq(deliv.voice)}`)
  if (deliv.voice && deliv.sfx) loud.push(`--sfx ${shq(deliv.sfx)}`)
  const runs = [
    'dead_frames {video}',
    `endcard {video} ${shq(words)}`,
    loud.join(' '),
    ...deliv.projectDirs.map((d) => `legibility ${shq(d)}`),
  ]
  return [
    `python3 ${shq(`${P}/evidence/evidence.py`)} report ${shq(deliv.mp4)} --out ${shq(evDir)} --words ${shq(words)}`,
    ...runs.map((r) => `--run ${shq(r)}`),
  ].join(' ')
}

function gatePrompt(cfg, st, n, deliv) {
  const evDir = `${st.dir}/a${n}/evidence`
  const body = [
    `TASK: run the preflight gates for style ${st.key}, attempt a${n}. Run exactly this one command once (it runs preflight/dead_frames, endcard, loudness and legibility and writes contact sheets). Change nothing, fix nothing and do not re-run it with other settings:`,
    gateCommand(cfg, deliv, evDir),
    'Exit 0 = every check passed, exit 1 = at least one failed; both are normal. Any other exit: return ok false with the last lines of stderr as error.',
    `Then read ${evDir}/preflight_report.json and return: ok true, passed (the report's "passed"), reportPath (${evDir}/preflight_report.json), sheetsJson (${evDir}/sheets.json), checks (one entry per report check, in report order: name, passed, kinds = the distinct finding kinds, detail = the first two finding details joined by "; " or ""), t0, t1, resumed.`,
  ].join('\n')
  return withReceipt('gate', st.dir, `a${n}.gate`, body)
}

function gateSummary(g) {
  if (!g || !Array.isArray(g.checks)) return 'no gate result'
  return g.checks.filter((c) => !c.passed).map((c) => `${c.name} [${c.kinds.join(',')}]: ${c.detail}`).join(' | ') || 'none'
}

function priorWork(st) {
  const d = st.deliv
  return `PREVIOUS ATTEMPT a${st.attempt}: ${st.dir}/a${st.attempt}/ (mp4 ${d.mp4}; projects ${d.projectDirs.join(', ')}; content ${d.contentJson || 'none'}; builder notes: ${d.notes})`
}

function segmentsNote(st, dir) {
  return `SEGMENTS (full mode): ${st.segments.map((s) => `#${s.index} ${fmt(s.t_start)}-${fmt(s.t_end)} s ${s.mp4} (project ${s.projectDir})`).join('; ')}.\nSTEPS: rebuild only the segments your fixes touch, each into a new dir ${dir}/segNN (same slicing, build, render and trim rules as the segment builders; edit only files inside the new projects); reuse the other segment mp4s by path. Then write ${dir}/concat.txt and concatenate all segments in order into ${dir}/look.mp4 (ffmpeg -hide_banner -n -f concat -safe 0 -i concat.txt -c:v libx264 -crf 16 -pix_fmt yuv420p -r 30 -c:a aac -b:a 256k look.mp4, run in ${dir}). Return every segment project in projectDirs and every segment mp4 in segments, both in segment order.`
}

function fixPrompt(cfg, st, n) {
  const dir = `${st.dir}/a${n}`
  const g = st.gate
  const body = [
    `TASK: the preflight gates failed for style ${st.key} attempt a${st.attempt}. Fix the failures as attempt a${n}. You are the builder.`,
    narration(cfg),
    craft(cfg, st, portFor(cfg, st.key, st.index, 0)),
    priorWork(st),
    `FAILED CHECKS (full spans in ${g.reportPath}): ${gateSummary(g)}.`,
    'GATE RULES: dead_frames = no black or frozen span of 0.5 s or more; endcard = end card visible at least 1.5 s after the last word, never black, not covering the last word; loudness = voice -14 LUFS +-1, true peak <= -1.5 dBTP, SFX at least 12 dB under the voice peak; legibility = type at least 44 px at 1080 px width and no empty lower third.',
    st.segments ? segmentsNote(st, dir) : `STEPS: rebuild with build.py into ${dir}/build (you may write a changed ${dir}/content.json), edit only files inside that new project to fix each failed check, render to ${dir}/look.mp4. Do not copy the previous project tree. Keep everything that already passed.`,
    DELIV_RETURN,
  ].filter(Boolean).join('\n')
  return withReceipt('fix', st.dir, `a${n}.fix`, body)
}

function criticPrompt(cfg, st, n) {
  const g = st.gate
  const P = cfg.pluginRoot
  const body = [
    `TASK: score style ${st.key}, attempt a${n} of run ${cfg.runTag}. You did NOT build it. Write nothing except the receipt (and, if you need them, extra contact sheets under ${st.dir}/a${n}/critic-sheets/).`,
    `SCORING STANDARD: Read ${P}/critic/rubric.md in full and apply it exactly: its anchors, the preflight cap, the category slugs and the output shape.`,
    'EVIDENCE (view the images; do not just list them):',
    `- preflight report: ${g.reportPath} (every check result and finding)`,
    `- contact sheets: every PNG listed in ${g.sheetsJson}; view each with Read`,
    `- the video: ${st.deliv.mp4} (extra sheets: python3 ${P}/evidence/evidence.py sheets <mp4> --out ${st.dir}/a${n}/critic-sheets --words ${cfg.clip.words})`,
    `- ${narration(cfg)}`,
    `- the registry entry for style ${st.key} in ${P}/styles/styles.json`,
    `- the reference reel stills in ${cfg.studioRoot}/_kit/lookdev/inputs/references/, only when you weigh a 9 or above; if that directory does not exist, skip this check and score at most 8, because a 9 needs the reel`,
    'RETURN: score (1-10, half points allowed), mustFix (each item starts with a rubric category slug and a time span, e.g. "busy_frame 4.2-6.0 s: ..."), keep, t0, t1, resumed.',
  ].join('\n')
  return withReceipt('critic', st.dir, `a${n}.critic`, body)
}

function revisePrompt(cfg, st, n) {
  const dir = `${st.dir}/a${n}`
  const c = st.critic
  const body = [
    `TASK: revise style ${st.key} as attempt a${n} (revision ${st.revisions + 1} of at most ${cfg.maxRevise}). An independent critic scored attempt a${st.attempt} ${c.score}/10 against ${cfg.pluginRoot}/critic/rubric.md; 7 is shippable.`,
    narration(cfg),
    craft(cfg, st, portFor(cfg, st.key, st.index, 0)),
    priorWork(st),
    `MUST FIX: ${c.mustFix.map((m, k) => `${k + 1}. ${m}`).join(' ')}`,
    `KEEP: ${c.keep.join('; ') || 'none listed'}`,
    `PREFLIGHT FAILURES STILL OPEN: ${st.gatePassed ? 'none' : gateSummary(st.gate)}.`,
    st.segments ? segmentsNote(st, dir) : `STEPS: rebuild with build.py into ${dir}/build (you may write a changed ${dir}/content.json), apply every must-fix you can by editing only files inside that new project, render to ${dir}/look.mp4, and view a few frames to confirm. Do not copy the previous project tree.`,
    'Do not trade a passing preflight check for a fix; the gates run again before the re-score.',
    DELIV_RETURN,
  ].filter(Boolean).join('\n')
  return withReceipt('revise', st.dir, `a${n}.revise`, body)
}

function planPrompt(cfg) {
  const body = [
    `TASK: write the scene plan for a full-length short, once, for run ${cfg.runTag} (styles ${cfg.styles.join(', ')}). The plan is style-agnostic; per-segment builders apply it in each style.`,
    narration(cfg),
    'STEPS:',
    `1. ffprobe the wav duration. Find sentence ends in the words file and silence gaps with ffmpeg silencedetect (noise -30dB, d=0.05). Word end times lag the audio, so use the gaps for cut points and the words only to say which sentence a gap follows.`,
    `2. Split the narration into at most ${cfg.maxSegments} contiguous segments of 8-45 s, each boundary inside a silence gap after a sentence end. Segment 0 starts at 0; each segment starts where the previous ends; the last ends at the wav duration.`,
    '3. Per segment: summary (what is said) and visual (style-agnostic intent: what is on screen, hierarchy, emphasis words, where the beat lands). Keep each frame calm enough to read: one idea per beat.',
    `4. Write the plan JSON to ${cfg.runDir}/plan.json (new file).`,
    'RETURN: ok, durationSec, lastWordEnd, transcript (the full text), segments [{index, t_start, t_end, summary, visual}], planPath, t0, t1, resumed.',
  ].join('\n')
  return withReceipt('plan', cfg.runDir, 'plan', body)
}

function segmentPrompt(cfg, st, n, seg, total) {
  const dir = `${st.dir}/a${n}/seg${String(seg.index).padStart(2, '0')}`
  const len = seg.t_end - seg.t_start
  const last = seg.index === total - 1
  const body = [
    `TASK: build segment ${seg.index + 1} of ${total} of style ${st.key} (run ${cfg.runTag}, attempt a${n}): narration span ${fmt(seg.t_start)}-${fmt(seg.t_end)} s (${fmt(len)} s).`,
    `SCENE PLAN: ${seg.summary} VISUAL: ${seg.visual}`,
    `NARRATION SOURCE: wav ${cfg.clip.wav}; words ${cfg.clip.words}.`,
    craft(cfg, st, portFor(cfg, st.key, st.index, seg.index + 1)),
    'STEPS:',
    `1. Slice the voice: ffmpeg -hide_banner -n -i ${cfg.clip.wav} -af atrim=start=${fmt(seg.t_start)}:end=${fmt(seg.t_end)},asetpts=PTS-STARTPTS -c:a pcm_s24le ${dir}/voice.wav. Write ${dir}/words.json: the words with start >= ${fmt(seg.t_start)} and start < ${fmt(seg.t_end)}, times shifted by -${fmt(seg.t_start)}, ends clamped to ${fmt(len)}.`,
    `2. Build with build.py into ${dir}/build from that slice (content tailored to this segment's plan), then render to ${dir}/render.mp4.`,
    last
      ? `3. This is the final segment: keep its end card. Use ${dir}/render.mp4 as the segment.`
      : `3. This is not the final segment, so no end card may appear inside it. The kit card starts at lastWordEnd + 0.1 s, which is before ${fmt(len)} s, so before rendering disable it in the generated project (e.g. a .ka-endcard { display: none !important } rule in the page <style>, or the template's own option if it has one). Then trim the render to exactly ${fmt(len)} s with ffmpeg -hide_banner -n -i ${dir}/render.mp4 -t ${fmt(len)} -c:v libx264 -crf 16 -pix_fmt yuv420p -c:a aac ${dir}/segment.mp4, and check a frame near ${fmt(Math.max(0, len - 0.2))} s shows no card.`,
    `RETURN: ok, error, index ${seg.index}, mp4 (the segment file), projectDir, durationSec (ffprobe of the segment), notes, t0, t1, resumed.`,
  ].join('\n')
  return withReceipt('segment', st.dir, `a${n}.seg${String(seg.index).padStart(2, '0')}`, body)
}

function assemblePrompt(_cfg, st, n, segs) {
  const dir = `${st.dir}/a${n}`
  const list = segs.map((s) => `file ${shq(s.mp4)}`).join('\n')
  const body = [
    `TASK: assemble the ${segs.length} segments of style ${st.key}, attempt a${n}, into one video. Change nothing else.`,
    `1. Write ${dir}/concat.txt (new file) with exactly these lines:`,
    list,
    `2. Run: cd ${shq(dir)} && ffmpeg -hide_banner -n -f concat -safe 0 -i concat.txt -c:v libx264 -crf 16 -pix_fmt yuv420p -r 30 -c:a aac -b:a 256k look.mp4`,
    '3. ffprobe the result and confirm its duration is the sum of the segment durations (within 0.2 s).',
    `RETURN: ok, error, mp4 (${dir}/look.mp4), projectDirs (in segment order: ${segs.map((s) => s.projectDir).join(', ')}), segments (the segment mp4s above, in order), voice "", sfx "", contentJson "", notes (the measured duration), weaknesses [], t0, t1, resumed.`,
  ].join('\n')
  return withReceipt('assemble', st.dir, `a${n}.assemble`, body)
}

// ---------------------------------------------------------------------------------------------
// per-style state machine. next() returns the calls for the next step; apply() consumes results.

function newStyle(cfg, key, index) {
  return {
    key, index, dir: `${cfg.runDir}-${key}`,
    phase: cfg.mode === 'full' ? 'segments' : 'build',
    attempt: 0, deliv: null, gate: null, gatePassed: false, gateEval: null,
    cycleFixes: 0, gateFixes: 0, revisions: 0, critic: null, scores: [],
    segments: null, status: null, error: '',
  }
}

function call(role, st, label, attempt, prompt, schema) {
  const r = ROLES[role]
  return { role, style: st ? st.key : '-', attempt, label, prompt, opts: { label, phase: r.phase, schema, model: r.model, effort: r.effort } }
}

function nextCalls(cfg, st) {
  const k = st.key
  switch (st.phase) {
    case 'build':
      return [call('build', st, `build:${k}#a0`, 0, buildPrompt(cfg, st, 0), DELIV)]
    case 'segments':
      return cfg.plan.segments.map((seg) => call('segment', st, `segment:${k}#a0.s${seg.index}`, 0, segmentPrompt(cfg, st, 0, seg, cfg.plan.segments.length), SEG))
    case 'assemble':
      return [call('assemble', st, `assemble:${k}#a0`, 0, assemblePrompt(cfg, st, 0, st.segments), DELIV)]
    case 'gate':
      return [call('gate', st, `gate:${k}#a${st.attempt}`, st.attempt, gatePrompt(cfg, st, st.attempt, st.deliv), GATE)]
    case 'fix':
      return [call('fix', st, `fix:${k}#a${st.attempt + 1}`, st.attempt + 1, fixPrompt(cfg, st, st.attempt + 1), DELIV)]
    case 'critic':
      return [call('critic', st, `critic:${k}#a${st.attempt}`, st.attempt, criticPrompt(cfg, st, st.attempt), CRIT)]
    case 'revise':
      return [call('revise', st, `revise:${k}#a${st.attempt + 1}`, st.attempt + 1, revisePrompt(cfg, st, st.attempt + 1), DELIV)]
    default:
      return []
  }
}

function fail(st, msg) {
  st.phase = 'done'
  st.status = 'error'
  st.error = msg
}

function delivOk(d) {
  return d && d.ok === true && typeof d.mp4 === 'string' && d.mp4 && Array.isArray(d.projectDirs) && d.projectDirs.length > 0
}

function applyResults(cfg, st, results) {
  const r = results[0]
  switch (st.phase) {
    case 'build':
    case 'fix':
    case 'revise': {
      const was = st.phase
      if (!delivOk(r)) return fail(st, `${was} a${was === 'build' ? 0 : st.attempt + 1}: ${(r && r.error) || 'no deliverable'}`)
      if (was !== 'build') st.attempt += 1
      if (st.segments && Array.isArray(r.segments) && r.segments.length === st.segments.length && r.projectDirs.length === st.segments.length) {
        st.segments = st.segments.map((s, i) => ({ ...s, mp4: r.segments[i], projectDir: r.projectDirs[i] }))
      }
      if (was === 'fix') { st.cycleFixes += 1; st.gateFixes += 1 }
      if (was === 'revise') { st.revisions += 1; st.cycleFixes = 0 }
      st.deliv = r
      st.phase = 'gate'
      return
    }
    case 'segments': {
      const bad = results.findIndex((s) => !s || s.ok !== true || !s.mp4 || !s.projectDir)
      if (bad >= 0) return fail(st, `segment ${bad}: ${(results[bad] && results[bad].error) || 'no result'}`)
      st.segments = results.map((s, i) => ({ index: i, mp4: s.mp4, projectDir: s.projectDir, t_start: cfg.plan.segments[i].t_start, t_end: cfg.plan.segments[i].t_end }))
      st.phase = 'assemble'
      return
    }
    case 'assemble':
      if (!delivOk(r)) return fail(st, `assemble: ${(r && r.error) || 'no deliverable'}`)
      st.deliv = r
      st.phase = 'gate'
      return
    case 'gate': {
      const ev = evaluateGate(r)
      if (!ev.ran) return fail(st, `preflight a${st.attempt}: ${ev.reason}`)
      st.gate = r
      st.gateEval = ev
      st.gatePassed = ev.passed
      st.phase = !ev.passed && st.cycleFixes < cfg.maxGateFixes ? 'fix' : 'critic'
      return
    }
    case 'critic': {
      if (!r || !validScore(r.score) || !Array.isArray(r.mustFix) || !Array.isArray(r.keep)) return fail(st, `critic a${st.attempt}: no valid score`)
      const score = capScore(r.score, st.gatePassed)
      st.critic = { ...r, score, rawScore: r.score }
      st.scores.push({ attempt: st.attempt, raw: r.score, score, gatePassed: st.gatePassed })
      if (score >= SHIP_SCORE) { st.phase = 'done'; st.status = 'shipped' }
      else if (st.revisions < cfg.maxRevise) st.phase = 'revise'
      else { st.phase = 'done'; st.status = 'below_bar' }
      return
    }
    default:
  }
}

function styleResult(st) {
  const last = st.scores[st.scores.length - 1]
  return {
    key: st.key,
    status: st.status,
    score: last ? last.score : null,
    rawScore: last ? last.raw : null,
    gatePassed: st.gateEval ? st.gatePassed : null,
    mp4: st.deliv ? st.deliv.mp4 : '',
    attemptDir: `${st.dir}/a${st.attempt}`,
    attempts: st.deliv ? st.attempt + 1 : 0,
    revisions: st.revisions,
    gateFixes: st.gateFixes,
    scores: st.scores,
    mustFix: st.critic ? st.critic.mustFix : [],
    keep: st.critic ? st.critic.keep : [],
    error: st.error,
  }
}

// ---------------------------------------------------------------------------------------------
// runner: issues calls in a fixed order and journals time and tokens per step

function spent() {
  return typeof budget !== 'undefined' && budget && typeof budget.spent === 'function' ? budget.spent() : null
}

const journal = []
let round = 0

function outcomeOf(r) {
  if (r === null || r === undefined) return 'null'
  if (r.ok === false) return 'not_ok'
  return 'ok'
}

function record(c, r, tokens, mode, pooled) {
  const t0 = r && typeof r.t0 === 'number' ? r.t0 : null
  const t1 = r && typeof r.t1 === 'number' ? r.t1 : null
  const rec = {
    seq: journal.length,
    round,
    style: c.style,
    step: c.role,
    attempt: c.attempt,
    label: c.label,
    model: c.opts.model,
    effort: c.opts.effort,
    t0,
    t1,
    wallS: t0 !== null && t1 !== null ? t1 - t0 : null,
    tokens,
    tokensMode: mode,
    pooledCalls: pooled,
    cachedLikely: mode === 'exact' && tokens === 0,
    resumed: !!(r && r.resumed),
    outcome: outcomeOf(r),
  }
  journal.push(rec)
  log(`journal ${JSON.stringify(rec)}`)
}

async function runCalls(calls, pooled) {
  round += 1
  if (!pooled || calls.length === 1) {
    const out = []
    for (const c of calls) {
      const before = spent()
      const r = await agent(c.prompt, c.opts)
      const after = spent()
      record(c, r, before !== null && after !== null ? after - before : null, 'exact', 1)
      out.push(r)
    }
    return out
  }
  // Barrier: parallel() starts the thunks in array order, so the call order is fixed.
  const before = spent()
  const out = await parallel(calls.map((c) => () => agent(c.prompt, c.opts)))
  const after = spent()
  const total = before !== null && after !== null ? after - before : null
  calls.forEach((c, i) => record(c, out[i], total, 'pooled', calls.length))
  return out
}

function usage(_cfg, start) {
  const end = spent()
  const byStep = {}
  for (const j of journal) {
    if (j.tokensMode !== 'exact' || j.tokens === null) continue
    byStep[j.step] = (byStep[j.step] || 0) + j.tokens
  }
  const t0s = journal.map((j) => j.t0).filter((x) => x !== null)
  const t1s = journal.map((j) => j.t1).filter((x) => x !== null)
  return {
    tokens: start !== null && end !== null ? end - start : null,
    tokensByStepExact: byStep,
    pooledRounds: [...new Set(journal.filter((j) => j.tokensMode === 'pooled').map((j) => j.round))].length,
    agentWallS: t0s.length && t1s.length ? Math.max(...t1s) - Math.min(...t0s) : null,
    stepWallS: journal.reduce((s, j) => s + (j.wallS || 0), 0),
    calls: journal.length,
  }
}

// ---------------------------------------------------------------------------------------------
// main

const tokensAtStart = spent()
phase('Prep')
let cfg
try {
  cfg = validateArgs(args)
} catch (e) {
  const msg = `Prep FAIL: ${e.message}`
  log(msg)
  return { ok: false, version: 2, mode: args && args.mode, fail: msg, results: [], journal: [], usage: usage(null, tokensAtStart) }
}
log(`prep ok: mode ${cfg.mode}, styles ${cfg.styles.join(',')}, ${cfg.parallel ? 'lockstep parallel' : 'sequential'}, maxRevise ${cfg.maxRevise}, maxGateFixes ${cfg.maxGateFixes}${cfg.mode === 'look' ? `, clip ${fmt(cfg.clip.durationSec)} s` : ''}`)

if (cfg.mode === 'full') {
  phase('Plan')
  const [plan] = await runCalls([call('plan', null, `plan:${cfg.runTag}`, 0, planPrompt(cfg), PLAN)], false)
  const bad = validatePlan(plan, cfg)
  if (bad) {
    const msg = `Plan FAIL: ${bad}`
    log(msg)
    return { ok: false, version: 2, mode: cfg.mode, runTag: cfg.runTag, runDir: cfg.runDir, fail: msg, clip: cfg.clip, plan: plan || null, results: [], journal, usage: usage(cfg, tokensAtStart) }
  }
  cfg.plan = plan
  cfg.clip = { ...cfg.clip, durationSec: plan.durationSec, lastWordEnd: plan.lastWordEnd, transcript: cfg.clip.transcript || plan.transcript }
  log(`plan: ${plan.segments.length} segments over ${fmt(plan.durationSec)} s`)
}

phase('Build')
const styles = cfg.styles.map((k, i) => newStyle(cfg, k, i))
if (cfg.parallel) {
  for (;;) {
    const active = styles.filter((s) => s.phase !== 'done')
    if (!active.length) break
    const batches = active.map((s) => ({ s, calls: nextCalls(cfg, s) }))
    const flat = batches.flatMap((b) => b.calls)
    const out = await runCalls(flat, true)
    let i = 0
    for (const b of batches) {
      applyResults(cfg, b.s, out.slice(i, i + b.calls.length))
      i += b.calls.length
    }
  }
} else {
  for (const s of styles) {
    while (s.phase !== 'done') applyResults(cfg, s, await runCalls(nextCalls(cfg, s), false))
  }
}

const results = styles.map(styleResult)
for (const r of results) log(`result ${r.key}: ${r.status} score ${r.score} (raw ${r.rawScore}, gate ${r.gatePassed ? 'pass' : 'fail'}) after ${r.revisions} revisions and ${r.gateFixes} gate fixes${r.error ? `; error: ${r.error}` : ''}`)
const u = usage(cfg, tokensAtStart)
log(`usage ${JSON.stringify(u)}`)
return {
  ok: results.every((r) => r.status !== 'error'),
  version: 2,
  mode: cfg.mode,
  runTag: cfg.runTag,
  runDir: cfg.runDir,
  clip: cfg.clip,
  plan: cfg.plan || null,
  results,
  journal,
  usage: u,
}
