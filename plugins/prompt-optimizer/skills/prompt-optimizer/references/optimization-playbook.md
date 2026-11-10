# Optimization Playbook

Detailed guidance for each of the 8 optimization steps in Phase 2. Read this file when entering Phase 2 to apply the right techniques to the current task.

Each technique includes the principle, a before/after example, and notes on when to skip it.

---

## Step 2.1 — Reframe Negatives as Positives

**Principle:** Negative instructions ("don't," "avoid," "never") activate the concept they're trying to suppress — like saying "don't think about a pink elephant." Positive framing tells the model what to do, which is more reliably followed. Current Claude models are trained for precise instruction following, making positive directives especially effective.

**Before:** "Don't use jargon or technical terms."
**After:** "Use plain language accessible to someone without specialized background in this field."

**Before:** "Don't make the report too long."
**After:** "Keep the report under 500 words, structured as an executive summary."

**Before:** "Avoid bullet points."
**After:** "Write in flowing prose paragraphs with natural transitions between ideas."

**When to skip:** If the user has explicitly used a negative framing that's precise and unambiguous (e.g., "exclude Section 3 from the analysis"), there's no need to reframe. The point is to replace vague negatives, not to rewrite precise exclusions.

---

## Step 2.2 — Add Motivation Where Purpose Is Non-Obvious

**Principle:** Claude generalizes from the *why* behind an instruction, not just the *what*. When the model understands the purpose, it can handle edge cases the user didn't anticipate. Anthropic specifically recommends this: providing context for a formatting preference produced better generalization than a rigid rule.

**Before:** "Keep it under 300 words."
**After:** "Keep it under 300 words — this will be used as an executive summary for busy decision-makers who scan quickly."

**Before:** "Use simple language."
**After:** "Use simple language — the audience includes non-native English speakers and people outside this technical domain."

**Before:** "Include specific numbers wherever possible."
**After:** "Include specific numbers wherever possible — this deliverable will be reviewed by investors who evaluate claims based on quantitative evidence."

**When to skip:** When the purpose is self-evident from the task description. "Write a haiku about spring" doesn't need a motivation clause — the format is the purpose.

---

## Step 2.3 — Specify Output Format and Structure

**Principle:** Claude defaults to concise, efficient output. If you want specific structure, length, or formatting, you need to say so explicitly. XML tags are Claude's native structural language — the model is specifically trained to interpret them as semantic boundaries.

**Apply when:** The user hasn't specified format, or the task would benefit from structural guidance.

**Techniques:**
- Wrap distinct content sections in descriptive XML tags: `<executive_summary>`, `<analysis>`, `<recommendations>`, `<next_steps>`
- Specify length expectations: "approximately 800 words" or "2-3 paragraphs per section"
- Match prompt style to desired output: if you want prose, write the prompt in prose; if you want structured output, structure the prompt
- For deliverables: specify the output file type and any template requirements

**When to skip:** When the user has already specified format clearly, or when the task is conversational and format doesn't matter.

---

## Step 2.4 — Inject Grounding Instructions When Factual

**Principle:** Hallucination rates drop dramatically when the model is explicitly told to ground responses in provided materials and flag gaps. Empirical evidence shows this can cut hallucination rates by more than half. Anthropic recommends giving Claude "permission to say I don't know" as one of the most effective single interventions.

**Standard grounding injection:**
"Base your response on the provided context and source materials. If the information needed to answer a point isn't available in the sources, state that explicitly rather than inferring or speculating."

**For document analysis tasks, add quote-first extraction:**
"Before synthesizing your analysis, extract the specific passages from the source material that are most relevant to each point. Then build your analysis from those passages."

**For research or multi-source tasks:**
"When presenting findings, attribute claims to specific sources. If sources conflict, note the disagreement rather than choosing one silently."

**When to skip:** Creative tasks, brainstorming, hypothetical scenarios, or any task where the user explicitly wants the model to speculate or imagine beyond provided materials.

---

## Step 2.5 — Decompose If Complex

**Principle:** Performance doesn't degrade linearly with complexity — it collapses past a threshold. When a prompt contains too many simultaneous objectives, compliance drops on ALL of them, including ones the model previously handled well. Anthropic recommends breaking complex work into subtask chains with structured handoffs.

**Complexity signals (decompose when you see these):**
- More than 3 distinct objectives in a single prompt
- More than 10 constraints or requirements
- Task requires both research AND analysis AND formatting in one pass
- Output needs to serve multiple audiences with different needs
- Task spans multiple domains (e.g., financial analysis + marketing strategy + operational planning)

**Decomposition pattern:**
```
<subtask_1>
[First objective with its own focused instructions]
Produce: [specific intermediate output]
</subtask_1>

<subtask_2>
Using the output from subtask 1:
[Second objective with its own focused instructions]
Produce: [specific intermediate output]
</subtask_2>

<final_synthesis>
Combine the outputs from previous subtasks into:
[Final deliverable specification]
</final_synthesis>
```

**When to skip:** Tasks with a single clear objective and fewer than 5 constraints. Decomposing simple tasks adds unnecessary overhead.

---

## Step 2.6 — Add Structured Reflection for Complex Tasks Only

**Principle:** Self-review improves structural quality (completeness, coherence, tone alignment) but does NOT improve factual accuracy. The model can reliably check "did I address all stated requirements?" but cannot reliably check "are my facts correct?" Reserve this for tasks where structural quality matters and the review criteria are clear.

**Note:** Older Claude models could react oddly to "think" and its variants; current ones don't, so use whichever reflective verb fits naturally. "Evaluate," "reflect," "consider," and "assess" all read cleanly.

**For analytical tasks:**
"Before delivering your final response, evaluate whether your analysis addresses the core question and whether any assumptions need to be called out."

**For multi-requirement tasks:**
"After drafting your response, review it against each stated requirement to confirm nothing was missed. If any requirement couldn't be fully addressed, note why."

**For writing deliverables:**
"Before finalizing, assess whether the tone, structure, and level of detail match what was requested. Adjust if needed."

**When to skip:** Simple factual questions, quick lookups, casual conversation, formatting-only tasks, or any task where adding a reflection step would noticeably delay a response the user expects quickly. Never use self-reflection for factual verification — that's not something the model can reliably do on its own output.

**In-prompt reflection vs. API-level extended thinking:** Step 2.6 covers in-prompt reflection — directing the model to evaluate its own draft before delivering. Claude models also support API-level extended thinking, a dedicated reasoning pass before the visible response. How it is switched on and sized differs between models and changes between releases, so take the parameter shape from the current API documentation, not from this file. The two are distinct:
- In-prompt reflection: nudges the model to self-check structure, completeness, tone. Low overhead.
- Extended thinking: enables deeper multi-step reasoning, especially for math, logic, and hard analytical tasks. Higher latency and token cost; most effective when the problem has a verifiable correct answer or requires working through competing hypotheses.
Prompt-level techniques (like Step 2.6) can complement extended thinking but don't replace it — and adding a reflection directive inside an extended thinking call may be redundant.

---

## Step 2.7 — Include Examples If Available

**Principle:** Examples are the single most powerful prompting technique — empirically shown to be more effective than any other single intervention. Claude pays extremely close attention to example details and will pick up patterns (both intended and unintended). Use real examples when available; never fabricate them.

**Sources of examples:**
- Previous outputs from the current conversation
- Files or templates in the project folder
- Earlier work the user points you to (a file, a past session's notes)
- The user's own writing samples if provided

**Format:**
```
<example>
[Input/context for the example]
[The output that represents good quality for this task]
</example>
```

**For multiple examples, wrap in an outer tag:**
```
<examples>
  <example>[Example 1]</example>
  <example>[Example 2]</example>
</examples>
```

**When to skip:** When no relevant examples exist and fabricating one would risk misleading the model. Also skip when the task is novel enough that past examples might constrain rather than guide.

---

## Step 2.8 — Trim and Focus

**Principle:** More constraints don't equal better results. Compliance drops as simultaneous requirements increase, and Claude's precise instruction following means it may over-index on constraints you didn't intend to prioritize. The strongest prompts are clean, focused, and free of redundancy. Anthropic specifically warns that Claude may "overtrigger" on aggressive language — less is more.

**Trimming checklist:**
- Remove duplicate or overlapping instructions (if two constraints say similar things, consolidate into one)
- Remove constraints that are obvious from context (don't tell Claude to "be helpful" — it already is)
- Remove constraints inherited from the user's raw input that conflict with each other
- If total constraint count exceeds 10, prioritize by impact and cut the rest

**Priority order when cutting:**
1. Context and source material (highest — this is what the model reasons from)
2. Examples (second highest — these calibrate quality)
3. Format and structure instructions (third — these shape the output)
4. Constraints and guardrails (lowest — keep only the most impactful)

**When to skip:** When the prompt is already lean and focused. Not every prompt needs trimming.

---

## Reminder: Anti-Patterns

The parent SKILL.md contains the full list of debunked techniques with explanations. If you're unsure whether a technique is valid, check the "What This Skill Does Not Do" section there. The short version: role prompts, emotional appeals, blanket chain-of-thought, and factual self-grading are all empirically ineffective on frontier models.
