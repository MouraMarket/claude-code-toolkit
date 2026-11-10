---
name: prompt-optimizer
description: >-
  Improve a rough prompt or request before doing the task, and challenge the reasoning behind it.
  Use when someone says "optimize this prompt," "improve my prompt," "help me think through this,"
  "review my approach," or "make this request better," or when a high-stakes ask is genuinely
  ambiguous (external audience, money, or hard to undo, with gaps in who it is for or what good
  looks like). Do NOT use when another installed skill owns the task (use that skill), for
  questioning a plan with no task to run, for lookups, greetings, or trivial edits, or to teach
  prompting (use claude-teacher if it is installed).
---

# Prompt Optimizer

A pre-processor for rough or high-stakes requests: it runs before the task when Step 1.1 says to engage, or when someone asks for it. Three phases: **Classify → Optimize → Route & Execute**, with a cross-cutting behavior: **Critical Thinking Partner** — evaluating the user's reasoning for gaps, unstated assumptions, and better alternatives.

---

## Phase 1: Classify & Detect

Two evaluations happen together: task stakes and ambiguity.

### Step 1.1 — Decide whether to engage

**High-stakes means at least one of: an external audience, real money, or something hard to undo.** Count how many of the five signals below are present.

| Signal | Counts (yes) | Does not count (no) |
|---|---|---|
| **Reversibility** | Hard to undo: client deliverable, published post, sent email, live ad change | Draft, brainstorm, lookup |
| **Audience** | External: client, investor, public | Internal or personal use |
| **Complexity** | Several objectives or 10+ constraints, or it crosses domains | One objective, few constraints |
| **Domain sensitivity** | Budget, pricing, legal, medical, or strategy that moves money | General knowledge, creative, routine ops |
| **Output specificity** | Rigid format, length, tone, or template required | Format is flexible |

**Scoring:**
- 0–1 signals: stand down. Do the task as asked, with no optimization or commentary, unless the person asked for this skill, or the ask is high-stakes (external audience, money, or hard to undo) and genuinely ambiguous: then run Step 1.2.
- 2–3 signals: Medium. Optimize, then state assumptions after delivering.
- 4–5 signals: High. Present the approach for approval before executing.
- An explicit review ask ("review my approach"): present for approval whatever the score.

### Step 1.2 — Scan for Ambiguity

**Only if Step 1.1 engaged.** If it stood down, skip the rest of this skill and just do the task.

Check six dimensions:

1. **Context** — Is the background clear? Who is this for?
2. **Scope** — Broad topic or specific deliverable?
3. **Format** — Expected output type clear?
4. **Depth** — Overview or deep-dive?
5. **Tone/Style** — Communication register clear? (often inferable from project context)
6. **Success criteria** — Would the user recognize good output from bad?

**Resolution rules:**
- 0–1 ambiguous + low stakes → Proceed with silent assumptions.
- 2–3 ambiguous + low stakes → Proceed, state assumptions at the start.
- 2+ ambiguous + medium/high stakes → Ask 2–3 targeted clarifying questions before executing.
- Things inferable from project context, conversation history, or common sense don't count as ambiguous.

**Tiebreaker — ambiguity vs. stakes:** When both rules apply (e.g., medium-stakes task with 2+ ambiguities), ambiguity takes precedence. Questions block execution. Stakes only govern whether you present an approval gate (high) or note assumptions (medium) — not whether you proceed at all.

### Step 1.3 — Critical Thinking Check (Stage 1)

Does this task involve a decision, strategy, analysis, or non-trivial reasoning where the user's assumptions materially affect the outcome?

- **Yes + medium/high stakes** → Evaluate for unstated assumptions, logical gaps, scope blind spots, or confirmation bias. Deliver 1–2 targeted challenges alongside any clarifying questions.
- **Simple execution request** → Skip entirely.

On high-stakes tasks with complex reasoning, read `references/critical-thinking-guide.md` for the full challenge framework (what to look for, how to phrase it, when to back off). For straightforward medium-stakes tasks, the guidance above is enough.

---

## Phase 2: Optimize the Prompt

Apply these techniques in order; skip any that don't apply. On complex prompts, read `references/optimization-playbook.md` for detailed guidance, before/after examples, and the research basis.

1. **Reframe negatives as positives.** Rewrite "don't" / "avoid" instructions into descriptions of the desired behavior. Vague negatives suppress poorly; positive directives are followed precisely.

2. **Add motivation where purpose is non-obvious.** Add a brief "because..." clause when the model needs to understand *why* an instruction exists in order to generalize correctly across edge cases.

3. **Specify output format and structure.** Infer format from context when unstated. Use XML tags to wrap distinct sections. Match prompt style to desired output style — prose begets prose, structure begets structure.

4. **Inject grounding when factual.** For tasks involving claims, data, or analysis: instruct to base responses on provided materials and flag gaps rather than speculate. This is one of the highest-impact single interventions for reducing hallucination — when in doubt, add it.

5. **Decompose if complex.** If the task has >3 distinct objectives or >10 constraints, break into sequential subtasks with XML-tagged handoffs. Performance collapses past a complexity threshold — it doesn't degrade gradually.

6. **Add structured reflection for complex tasks only.** For analysis, strategy, or multi-step reasoning: add a reflection directive using "evaluate," "reflect," or "assess." Skip for simple tasks; it adds latency without benefit.

7. **Include examples if available.** Reference past outputs, templates, or writing samples in `<example>` tags. Examples are the single most powerful technique in prompting research. Only use real examples — fabricating them risks mis-calibrating the output.

8. **Trim and focus.** Remove redundant instructions, consolidate overlapping constraints, keep total count under ~10. When cutting, protect context and examples over constraints — the model reasons from context, not from rules.

---

## Phase 3: Route & Execute

### Step 3.1 — Check for downstream skills

If the optimized task maps to another skill that is actually installed in this session (it appears in your available skills list), pass the optimized prompt to that skill as input. Never route to a skill you cannot see: if none fits, do the task yourself with the optimized prompt.

### Step 3.2 — Execute or present

**Low stakes** (only when someone asked for this skill on a 0–1 signal task): Execute directly. The user sees only the final output.

**Medium stakes:** Execute the task, then include a brief note:
> "I interpreted this as [X] and optimized for [Y]. If you had a different intent, let me know."

**High stakes:** Present the approach before executing:

```
Here's how I'd approach this:

Task interpretation: [1-2 sentences]
Key assumptions: [anything inferred, not stated]
Approach: [the restructured plan]

Proceed with this, or adjust anything?
```

### Step 3.3 — Critical Thinking Check (Stage 2)

After delivering the final output, evaluate: is there a meaningfully different approach, a notable limitation, or a way to strengthen the user's position that hasn't been surfaced?

- **Yes** → Add 1–3 sentences after the deliverable, clearly labeled ("One thing to consider:" or "Alternative angle:"). Place it after the work, not before.
- **No genuine alternative** → Skip entirely.

For the full Stage 2 framework, read `references/critical-thinking-guide.md`.

---

## Cross-Cutting: Project Context

This skill is project-agnostic. When project context documents exist:

1. **Detect on first use.** Scan for: who the user/company is, typical audience, preferred tone, decision-making frameworks, explicit guardrails.
2. **Apply as silent defaults.** Use them to reduce clarifying questions and auto-populate format, tone, and success criteria when the user hasn't specified these.
3. **Override without friction.** If the user states a preference that contradicts project defaults, follow the user.

With no project context, expect fewer defaults and possibly 1–2 more clarifying questions on complex tasks.

---

## What This Skill Does Not Do (And Why)

- **Role prompts for accuracy.** Research shows role assignment doesn't improve factual accuracy on frontier models — it only shapes tone and register. Don't add "you are an expert in X" hoping for better facts, and never add one silently. Users often expect them: if a user explicitly requests "act as an expert in X," honor it as a tone directive, not an accuracy measure. That is the only case where a role prompt is appropriate.
- **Emotional appeals or motivational language.** "This is very important" and "I'll tip you $100" have been tested on frontier models with no measurable effect. Structural clarity is what moves the needle.
- **Blanket chain-of-thought.** Forcing "think step-by-step" on every task adds significant latency and actually hurts performance on classification, factual recall, and pattern tasks. Reserve it for genuinely complex reasoning. (API-level extended thinking is separate — playbook Step 2.6.)
- **Sampling parameters as creativity/precision dials.** Some recent Claude models don't accept `temperature`, `top_p` or `top_k` at all, and which ones do changes between releases, so check the current model documentation before suggesting one. Use prompting to guide output style instead: specificity of instructions, examples, and grounding instructions replace what sampling knobs used to do.
- **Factual self-grading.** The model can check structural completeness ("did I address all requirements?") but cannot reliably verify its own facts. Self-review is for structure and tone only.
- **Manufacturing disagreement.** The critical thinking partner behavior exists to surface genuine concerns, not to perform intellectual rigor. If there's nothing worth flagging, flag nothing.
- **Holding output hostage.** Challenges and alternatives are delivered alongside or after the work product. Commentary accompanies; it doesn't gate.
- **Overriding explicit instructions.** If the user specified something, respect it. The skill may flag an alternative after delivery, but the user's stated intent takes precedence.

---

## Gotchas

Accumulated failure patterns. Add a line each time the skill produces a bad outcome.

- Over-optimization on low-stakes tasks adds friction the user doesn't want. When in doubt, classify lower.
- The critical thinking check can feel like "Claude is arguing with me" on medium-stakes tasks. Keep challenges to 1-2 sentences max, and only when genuinely warranted.
- Decomposition (technique #5) is rarely needed for tasks under 200 words.

---

## Worked Example

**User input:** "Write me an email to our investors explaining why we're pivoting from B2C to B2B."

**Phase 1:** High stakes (4 signals: audience, reversibility, domain, complexity). Ambiguous tone and depth. Clarifying questions + Stage 1 challenge about "pivot" framing.

**Phase 2 optimizations:** Motivation (investors evaluating backing), format (email with subject line), grounding (use user's specifics, don't fabricate), reflection (would an investor feel reassured or alarmed?).

**Phase 3:** Execute, then Stage 2 suggestion: follow with an FAQ for obvious investor questions.
