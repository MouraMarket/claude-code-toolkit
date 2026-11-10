export const meta = {
  name: 'blinded-review',
  description: 'One blinded reviewer per /build gate; execution is a returned field, not a hope',
  phases: [{ title: 'Blinded review', detail: 'one Opus reviewer at max, blinded to the author' }],
}

const MERGE_REVIEW_REPORT = {
  type: 'object', required: ['recommendation', 'executed', 'findings'], additionalProperties: false,
  properties: {
    recommendation: { type: 'string', enum: ['APPROVE', 'REQUEST_CHANGES', 'COMMENT'] },
    // "Nothing was executed" is a RESULT the
    // architect has to read, not a silence. As an
    // instruction it was a hope; as a required field it cannot be skipped
    // quietly. `why_not` is required unconditionally, and the post-dispatch check
    // below rejects it blank when `ran` is false (a required string may still be
    // empty, so the schema alone does not): a repository with no suite has
    // nothing to run, so a blank here would routinely read as expected noise
    // and stop being read.
    executed: {
      type: 'object', required: ['ran', 'commands', 'why_not'], additionalProperties: false,
      properties: {
        ran:      { type: 'boolean' },
        commands: { type: 'array', items: { type: 'string' },
                    description: 'Exactly what was run, verbatim, so the claim is checkable in one command.' },
        why_not:  { type: 'string',
                    description: 'If ran is false, why — no suite in this repo, docs-only diff, or it could not be run. If ran is true, what you falsified by hand in addition.' },
      },
    },
    findings: { type: 'array', items: {
      type: 'object', required: ['where', 'defect', 'consequence', 'severity', 'evidence'], additionalProperties: false,
      properties: {
        where:       { type: 'string' },
        defect:      { type: 'string' },
        consequence: { type: 'string' },
        severity:    { type: 'string', enum: ['BLOCKING', 'IMPORTANT'] },
        // Triage key: a BLOCKING with empty `evidence` is ASSERTED, not
        // demonstrated. Fix the demonstrated ones first; the rest are advisory.
        evidence:    { type: 'string',
                       description: 'The failing test, repro command, or surviving mutation that demonstrates this. Empty string if you did not demonstrate it.' },
      } } },
  },
}

const input = typeof args === 'string' ? JSON.parse(args) : (args || {})
const { gate, briefPath, planPath, lensDir } = input

// Each gate's checklist lives in its reference, which is the single
// copy of the lens prompts; this script names the file and heading rather than
// restating them. test_build_lens.py asserts every named heading exists in the
// named file, so renaming a heading reddens a test instead of silently
// emptying a checklist. The dispatcher passes `lensDir`, the directory it read
// the reference from, so the path is the one that dispatcher could actually read.
const LENS_DIR = lensDir
const GATES = {
  design: { artifact: 'design', agentType: null,
            checklist: `the table under "### The lens table" in ${LENS_DIR}/design-review.md` },
  plan:   { artifact: 'plan', agentType: null,
            checklist: `the table under "### Plan Review lenses" in ${LENS_DIR}/plan-review.md` },
  code:   { artifact: 'change', agentType: 'toolkit-build:code-reviewer',
            checklist: `every lens table under "### Code Review lenses" in ${LENS_DIR}/code-review.md` },
  merge:  { artifact: 'change', agentType: 'toolkit-build:code-reviewer', checklist: null },
}

// Guard layer 1 of 3. An empty brief returns the exact shape of a clean
// review. This script receives PATHS and cannot read them, so it refuses
// every malformed call before any agent runs; the dispatch block in each
// reference runs `test -s` (layer 2), and the prompt tells the reviewer what
// to return for a file it cannot read (layer 3).
if (!Object.prototype.hasOwnProperty.call(GATES, gate)) {
  throw new Error(`gate must be one of ${Object.keys(GATES).join(', ')}; got ${JSON.stringify(gate)}`)
}
// A drive-letter path with forward slashes (`C:/Users/x/brief.md`) is absolute
// on Windows too. Backslash paths stay rejected -- the reviewer's tools and
// bash both take forward slashes on Windows, so a backslash spelling is never
// what this dispatcher hands the agent.
const isAbsolutePath = (p) => typeof p === 'string' && p.trim() === p && p.length > 1 &&
  (p.startsWith('/') || /^[A-Za-z]:\//.test(p))
if (!isAbsolutePath(lensDir)) {
  throw new Error(`lensDir must be an absolute path to the directory holding the gate references; got ${JSON.stringify(lensDir)}`)
}
if (!isAbsolutePath(briefPath)) {
  throw new Error(`briefPath must be an absolute path to the brief file; got ${JSON.stringify(briefPath)}`)
}
if (gate === 'code' && !isAbsolutePath(planPath)) {
  throw new Error(`gate "code" requires planPath, the absolute path to the plan; got ${JSON.stringify(planPath)}`)
}
if (gate !== 'code' && planPath !== undefined) {
  throw new Error(`planPath is accepted only at gate "code" -- every other gate is blinded to the plan`)
}

const spec = GATES[gate]
const UNREADABLE =
  'If any file named here is missing, unreadable or empty, do not review: return ' +
  'recommendation REQUEST_CHANGES, executed.ran false, executed.why_not naming the file, ' +
  'and exactly one BLOCKING finding naming it. Never return an empty findings list for a review you could not perform.'
const FRAMING = {
  design: `Review the design described in ${briefPath}. That file is your ONLY framing -- it holds ` +
          `the design and the requirements it answers. Do not read any other spec, plan, ledger or ` +
          `handover about it, and do not go looking for one.`,
  plan:   `Review the plan described in ${briefPath}. That file is your ONLY framing -- it holds ` +
          `the plan and the requirements it must satisfy. Do not read any other spec, design ` +
          `document, ledger or handover about it, and do not go looking for one.`,
  code:   `Review the change described in ${briefPath}. The plan it was built from is ${planPath}; ` +
          `it is the contract the change is judged against. Those two files are your ONLY framing -- ` +
          `do not read any spec, design document, ledger or handover, and do not go looking for one.`,
  merge:  `Review the change described in ${briefPath}. That file is your ONLY framing -- ` +
          `do not read the plan, the spec, or any design document, and do not go looking for one.`,
}
const EXECUTE = (gate === 'design' || gate === 'plan')
  ? `Where the ${spec.artifact} names a command, a file or a behaviour of existing code, run or read it to check the claim; report exactly what you ran.`
  : `Run the repo's suite and attempt to falsify the changed behaviour; report exactly what you ran.`
const CHECKLIST = spec.checklist
  ? `Your checklist is ${spec.checklist} Apply EVERY row. Assume the ${spec.artifact} is ` +
    `broken and find where.`
  : ''

const prompt = [FRAMING[gate], CHECKLIST, EXECUTE, UNREADABLE].filter(Boolean).join(' ')

phase('Blinded review')
// `model` and `effort` are explicit on every gate: an omitted model inherits
// the session's silently, and at design/plan there is no agent frontmatter to
// supply `effort: max`. `agentType` reuses agents/code-reviewer.md at code and
// merge rather than restating its prompt; design and plan use a plain agent
// because code-reviewer's description forbids it as the sole reviewer of a
// plan and its rubric is diff-ordered.
const opts = { label: `blinded-review:${gate}`, model: 'opus', effort: 'max', schema: MERGE_REVIEW_REPORT }
if (spec.agentType) opts.agentType = spec.agentType
const report = await agent(prompt, opts)
if (report == null) {
  throw new Error(`the ${gate} reviewer returned nothing -- this gate did not run`)
}

// The semantic rule, in JavaScript (`semantic_violation` in
// tools/build-lens/review_lib.py is the Python copy the Agent runtime uses).
// A structurally valid report can still describe a review that never
// happened -- the schema alone cannot catch it. These checks are one rule in
// two languages; change them together. Both copies are tested against
// tools/build-lens/tests/semantic_cases.json.
const executed = report.executed
if (executed == null || typeof executed !== 'object' || Array.isArray(executed)) {
  throw new Error(`the ${gate} reviewer returned a report this gate cannot accept: executed is missing or not an object`)
}
if (executed.ran === false) {
  if (!Array.isArray(report.findings) || report.findings.length === 0) {
    throw new Error(`the ${gate} reviewer returned a report this gate cannot accept: executed.ran is false and findings is empty -- indistinguishable from a broken gate`)
  }
  if (typeof executed.why_not !== 'string' || executed.why_not.trim() === '') {
    throw new Error(`the ${gate} reviewer returned a report this gate cannot accept: executed.ran is false and executed.why_not is empty or whitespace`)
  }
}
if (executed.ran === true) {
  if (!Array.isArray(executed.commands) || executed.commands.length === 0) {
    throw new Error(`the ${gate} reviewer returned a report this gate cannot accept: executed.ran is true and executed.commands is empty`)
  }
  if (executed.commands.some((c) => typeof c !== 'string' || c.trim() === '')) {
    throw new Error(`the ${gate} reviewer returned a report this gate cannot accept: executed.ran is true and executed.commands has a blank entry -- a command nobody can re-run`)
  }
}
if (report.recommendation === 'APPROVE' && Array.isArray(report.findings)) {
  const blocking = report.findings.filter((f) => f != null && typeof f === 'object' && f.severity === 'BLOCKING').length
  if (blocking > 0) {
    throw new Error(`the ${gate} reviewer returned a report this gate cannot accept: recommendation is APPROVE but ${blocking} finding(s) are BLOCKING -- a report cannot approve what it says blocks the merge`)
  }
}
return report
