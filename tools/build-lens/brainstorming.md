# Phase 1 — the design process, by classification

`skills/builder/build/SKILL.md` Phase 1 classifies the work out loud; this file is what each
path then does. It is not a gate reference: it is the architect's own process,
read before any gate runs. Ported from superpowers 6.3.0 `brainstorming`,
adapted to `/build`: questions go through `AskUserQuestion`, the design is
attacked by Design Review before the user reads it, and approval is written down.

**Every path ends with the user approving your intent before implementation.**
What scales with the work is the artifact — two sentences in chat or a written
spec — never the approval.

**Every message in this phase reads like the closeout.** Use a table wherever
the content is a set of like things: approaches and their trade-offs, files
touched, premises marked verified or inferred. Keep prose to paragraphs of
about three lines. A decision the options alone carry goes in
`AskUserQuestion`. A decision the user must read a table, link or design to
answer goes in the turn's **final text message**, asking there: VS Code
collapses text before a tool call to one line, and an option preview is no
substitute. `AskUserQuestion` follows on the next turn only if a structured
choice is still needed.

## Architectural path

1. **Explore context** — files, docs, recent commits, the mission spec. If the
   request is several independent builds, say so before asking anything else
   and decompose: one design per build, each through this path.
2. **Clarifying questions** — one per message, through `AskUserQuestion`,
   multiple-choice where possible: purpose, constraints, what success looks
   like. Ask only the ones whose answer changes the design.
3. **Two or three approaches** — each with its trade-offs, your recommendation
   first and why. Cut anything the problem does not yet need. "Smaller" or "do
   nothing" is an option wherever it is credible.
4. **Present the design in sections** — each scaled to its complexity, from a
   few lines to about 150 words, with the user's approval after each:
   components, data flow, failure shape (would a defect announce itself?),
   testing.
5. **Write the design doc** — `<specs_dir>/YYYY-MM-DD-<slug>-design.md` (the
   directory `/build`'s Phase 0 resolved), with `status:` and `subject:` front
   matter. **No review findings, rulings or adjudication in it**: it is the
   contract Plan Review reads, and anything written there reaches that reviewer.
   Those go in the ledger.
   Also write `<specs_dir>/YYYY-MM-DD-<slug>-requirements.md`: the
   acceptance contract the design answers (what success looks like,
   constraints, explicit exclusions), same front matter, no review findings or
   rulings. It reaches both Design Review and Plan Review.
6. **Self-review** — placeholders, internal contradictions, scope (one plan's
   worth, or several?), ambiguity (could a requirement be read two ways?). Fix
   inline.
7. **Design Review** — as `design-review.md` in the same directory as this file describes,
   handing it the design and the requirements it answers. Check every finding
   against the repo; fixes go into the design text; findings and dispositions
   go in the ledger.
8. **The user reviews the written spec** — in the turn's final text message:
   the design file as a link, about two sentences on what it does, and one
   table of high-level callouts only (what Design Review found and what you
   did about it is one row). Ask for approval there. On their yes, add
   `Approved: YYYY-MM-DD` to the design's front matter. Changes requested:
   edit and re-run step 6. A **material** change — a new component,
   interface or failure behaviour — also re-runs step 7 once before you ask
   again; a wording-only edit does not.
9. **Phase 2** — the plan's header carries `Classification: architectural` and
   `Design: <design file>` and `Requirements: <requirements file>`, a bare
   filename when the file sits beside the plan.

## Bounded path

Explore the context, ask the clarifying questions that matter, then present a
short design in the turn's final text message — a table of the files touched,
then a few lines on the approach and how it is tested — and **stop until
the user says yes**. Presenting the design and starting in the same
breath is skipping the gate. No design doc. The plan's header carries
`Classification: bounded`.

## Spike path

State the question and what you will try in two or three sentences, and get a
nod. Find out as cheaply as correctness allows, and report a recommendation.
Anything you built stays labelled throwaway. No plan.

## Red Flags

`skills/builder/build/SKILL.md` carries the three that most often skip this file. These are
the rest:

| Thought | Reality |
|---|---|
| "It's bounded and the design is obvious — I'll start while they read it" | The gate is the approval, not the design's length. Present, then stop until you hear yes. |
| "I understand this kind of app, so it's bounded" | Bounded measures the repo, not your familiarity. No existing flow to change means architectural. |
| "It grew but I'm almost done" | The ratchet is one-way — a bigger build reclassifies, it does not grandfather in. |
| "They approved the spike so the follow-up is approved" | A spike answers one question; building on it needs its own yes. |
| "The spike works, so I'll keep the code" | A spike's output is an answer. Keeping the code is a new request — classify it. |

## Not ported

The source skill's visual companion (a local browser server for mockups). An
Artifact covers that need here.
