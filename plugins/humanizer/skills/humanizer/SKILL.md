---
name: humanizer
description: >-
  Strip AI-writing patterns from a draft and give it a human pulse: emails,
  reports, posts, newsletters, blog drafts, proposals, docs and notes. Two
  modes: edit a draft (default), or detect and name the patterns without
  rewriting. Trigger on "humanize this," "make this sound less like AI,"
  "de-slop this," "remove the AI tells," "is this AI slop," "this reads like
  ChatGPT," "why does this sound robotic," or after generating any draft that
  needs a human pass. If another skill sets a brand voice, run it first, then
  this. For copy published under someone else's name (a client's, an
  employer's brand), use only the pattern catalog: the pulse step would add
  opinions its owner never approved. Not for keyword lists, data output, or
  code.
---

# Humanizer

Two jobs, one standard: text that reads like a person wrote it, and nothing invented along the way.

## Scope: whose voice is this?

This skill injects voice. That makes it the right tool for some writing and the wrong tool for the rest, so settle this before editing a single line.

**Use it in full on writing in the user's own voice.** Their emails, docs and notes, reports they send, proposals, their own posts and newsletters, blog drafts. Here the failure mode is flatness. On pieces the user publishes as themselves, adding stance is the fix; on reports, proposals and emails sent to someone who will act on them, the fix is rhythm and plain wording, not new stance.

**Do not add voice to writing published under someone else's name.** A client's social captions, ads or website, an employer's brand copy, a ghostwritten piece. Adding opinions and personality to copy that goes out as someone else is a real review risk, because they never said those things.

For that copy, the voice belongs to its owner. Two ways to proceed:

- Use the pattern catalog only. Read `references/pattern-catalog.md` and strip the tells. Skip everything in "Give it a pulse" below. Pattern-stripping is voice-neutral and safe on any draft.
- Or, if the user has the owner's voice guidelines, apply those instead of this skill's pulse step.

When you cannot tell whose voice a draft is in, ask. One question: "Is this going out as you, or as someone else?"

## The two modes

**Edit (default).** The user hands over a draft to fix. Strip the patterns, restore a pulse, return the edited draft plus a short "What changed" section.

**Detect.** The user asks whether something reads as AI, or asks to audit, scan, or flag a draft without rewriting it. Name each pattern that appears, quote the line, give the fix in a few words. Then offer to edit.

Detect only when the person asks for a verdict or a list of patterns without a rewrite ("is this AI," "audit this," "don't rewrite"). "Why does this sound robotic" and "this reads like ChatGPT" are detect, then offer to edit. "Humanize," "fix" or "clean up," or a draft handed over with no question, is edit. In detect, read references/pattern-catalog.md and run the Detect section of `references/self-check.md`.

In detect mode, report named patterns rather than a score or a verdict on authorship. AI detectors guess and are wrong often enough to be worthless. A named pattern with a quoted line is evidence the user can check themselves, which is the whole point.

## Edit mode

### Step 1: read the whole draft first

Identify the core point and three to five voice signals worth keeping: vocabulary, cadence, bluntness, humor, admitted uncertainty, digressions. Keep this note internal. If the core point is unclear, ask instead of guessing, because an edit that sharpens the wrong point is worse than no edit.

If the audience or format is unclear and it changes the edit, ask one question: who is this for and where does it land?

### Step 2: strip the patterns

Work through `references/pattern-catalog.md`. It covers six families:

1. Inflation and puffery
2. Structural tics
3. Vagueness and evasion
4. Weak construction
5. Formatting tells
6. Openings and endings

The catalog opens with the word and phrase lists (banned outright, often-empty adverbs, often-empty phrases, filler swaps). Those are the fastest wins, so start there.

**Zero em dashes. Hard rule, no exceptions.** Every em dash gets replaced with a comma, period, colon, semicolon, or parentheses. This one is absolute rather than a matter of judgment, because it is the most reliable AI tell in the whole catalog and because a rule with a "when it clearly helps" escape hatch always leaks. The only survivor is an em dash inside quoted material, where removing it would misquote the source. En dashes used as sentence punctuation go too; numeric ranges stay.

### Step 3: give it a pulse

*Drafts in the user's own voice only. Skip this step entirely for copy published under someone else's name.*

Stripping patterns produces clean, voiceless text, and voiceless text is its own tell. A draft where every sentence is the same length, every claim is neutral, and nobody is visibly thinking reads as machine-made no matter how many banned words you removed.

Signs a draft is technically clean and still dead: uniform sentence length, no stated opinion, no acknowledged uncertainty or mixed feeling, no first person where first person fits, no humor or edge, reads like a press release.

On reports, proposals and emails sent to someone who will act on them, pulse means rhythm and plain wording only. Take no new position, add no causal claim, and keep the draft's own first person ("we" or "I"); never introduce one.

What to do about it:

- **Take a position.** React to facts rather than only reporting them. "I genuinely don't know how to feel about this" carries more weight than a balanced list of pros and cons.
- **Vary the rhythm.** Short punchy sentences. Then a longer one that takes its time getting where it is going. Mixed lengths read as thought; uniform lengths read as template.
- **Admit complexity.** Real people hold mixed views. "Impressive, and a little unsettling" beats "impressive."
- **Use "I" when it fits.** First person is honest, not unprofessional. "Here's what gets me about this" signals a person doing the thinking.
- **Let some mess in.** Perfect structure feels generated. An aside, a tangent, a half-formed thought all read as human.
- **Be specific about feeling.** Not "this is concerning" but "there's something unsettling about agents grinding away at 3am with nobody watching."

**The hard line on invention.** Adding voice means adding stance, rhythm, and personality. It never means adding facts. Do not invent claims, statistics, examples, quotes, names, dates, or sources, and do not upgrade a hedge into a certainty the draft did not earn. If a sentence needs a number or a source to land, ask the user for it. Fabricated specifics are the one edit that does real damage, because they survive review by looking exactly like the good kind of specificity.

### Step 4: keep the edit proportional

Match the amount of cutting to the actual amount of slop. Leave strong human sentences alone rather than rewriting them for consistency, and resist making every paragraph equally tidy. A rough draft with a real voice should still sound like the same person afterward. Preserve edge, blunt language, humor, profanity, self-interruption, and honest admission when they belong to the writer.

Keep the writer's structure and detours unless the structure is actively hurting the piece. If you reorganize, say why in "What changed."

### Step 5: run the two-question audit

This step catches what the catalog misses, so do it every time rather than only when the draft feels off.

Ask yourself, in writing: **"What makes the draft below so obviously AI generated?"** Answer in a few honest bullets. Name the remaining tells even when they are subtle: too-tidy contrast structures, evenly paced paragraphs, a closer that reads slogan-y, specifics that look plausible but unverified.

Then ask: **"Now make it not obviously AI generated."** Revise against your own answer.

### Step 6: verify against the self-check

Run the edited draft against `references/self-check.md` yourself. Answer each item pass or fail. Fix any failure and re-check. Do this inline rather than dispatching a separate reviewer, because the checks are short and a second pass on your own output catches them fine.

## Output format

1. The full edited draft
2. **What changed**: a short list of the substantive edits, patterns removed, and any reorganization with its reason
3. The two-question audit is thinking, not deliverable. Include it only when the user asked to see the reasoning, or when a remaining tell needs a decision from them.

For detect mode: the named patterns with quoted lines and short fixes, then an offer to edit. No rewrite, no score, no authorship claim.

## How this fits other skills

- **A voice or brand skill sets the voice; this strips the artifacts.** This skill works alone. If the user has a voice or brand skill installed and used it, run that first, then this.
- **A skill that drafts copy as someone else** (a client's captions, say) should take the pattern catalog only and skip "Give it a pulse," for the reason in Scope above.
- **`prompt-optimizer`**, if installed, is separate: it improves a request when someone asks it to, and needs to know nothing about this one.

## Keeping the catalog current

AI writing tells drift: old patterns get trained out and new ones appear. A frozen catalog quietly loses accuracy, so this skill needs a refresh path.

The most useful outside tracker is [Wikipedia:Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing), maintained by WikiProject AI Cleanup. Editors update it continuously from real AI-generated text, so it is a good early signal. Many of the patterns here were first listed there, and in the open-source [blader/humanizer](https://github.com/blader/humanizer) skill.

Use it as a source of *candidate* patterns, not as an authority this skill defers to. When refreshing:

1. Compare their current list against `references/pattern-catalog.md` and pull out anything genuinely new.
2. Check each candidate against writing you actually produce. A pattern common in encyclopedia articles may never appear in an email.
3. Add the ones that survive, with a before and after you write yourself from your own drafts, never copied from the source: its examples are someone else's text under someone else's licence.
4. Add a matching line to `references/self-check.md`, or the pattern will be documented and never checked.
5. Note anything a reader of your writing flagged that no outside list covers. Nobody else will catalog those.

The catalog is the skill's own. If the tracker conflicts with what reads well in your writing, your writing wins.

## Gotchas

Accumulated failure patterns. Add a line each time this skill produces a bad outcome.

- Over-editing is the most common failure. A draft can lose its voice to aggressive cleanup as easily as to AI generation, and the result is harder to spot because it scans well.
- Stripping every hedge makes writing sound falsely certain. Keep "I think," "maybe," and "to be honest" when they carry real uncertainty or the writer's spoken rhythm.
- The banned-word lists are guidance, not a find-and-replace. A banned word inside a quote, a brand's owned vocabulary, or a proper noun stays.
- Voice injection on a draft that already has voice makes it louder, not better. If step 1 found strong voice signals, step 3 is mostly a no-op.
- Fake-profound closing lines want to be rewritten into better metaphors. Delete them instead and end on the clearest concrete sentence already in the draft.
- Asking "whose voice is this?" feels like friction on an obvious personal note. Only ask when the answer changes the edit.
- **Some letters look like punctuation and are not.** The quote and word rules never touch them. The Hawaiian ʻokina (U+02BB, as in Hawaiʻi) is a letter, and so are diacritics such as the kahakō (ā); straightening or stripping one is a spelling error.
