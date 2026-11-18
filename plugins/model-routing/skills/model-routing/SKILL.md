---
name: model-routing
description: >-
  Use when a request means doing the same piece of work across many items, or several
  independent pieces at once, so Claude may split it across subagents: "for each of these 15
  companies," "research every competitor on this list," "summarize all 30 of these reviews,"
  "check every page on the site," "do these in parallel," "use subagents for this." Also when
  someone asks which model or effort level fits a task, or wants to use less of their limit:
  "is Opus overkill for this," "should I switch to Sonnet," "what effort should I use," "I keep
  hitting my usage limit," "make this cheaper to run." Do NOT use to explain what Claude's
  models are or how Opus, Sonnet and Haiku differ, with no task to fit them to: "explain Opus vs
  Sonnet," "what's the difference between the models" (use claude-teacher). Do NOT use to
  improve a prompt (use prompt-optimizer).
---

# Model routing

Pick the cheapest model and effort that still does each piece of work well, and say what that
trades away. This applies to two things: the **subagents** Claude dispatches, and the
**session's own** model and effort, which only the person can change in the model menu.

Usage limits are usually the constraint people hit first. A subagent dispatched with no model
setting runs on the session's model, often Opus or Fable, and pays the top rate for work
Sonnet or Haiku does just as well.

## 1. Check the session's own setting first

Read your model name from your own system prompt. Never state the person's effort level. You
may see a numeric effort value, but it doesn't map reliably to the menu's labels (Low, Medium,
High and up), so naming a label from it is a guess. Phrase effort advice as a condition: "if
you're on High or above, Medium is enough for this." Then make **one** suggestion, only where
it applies:

| You're on | And the work is | Suggest |
|---|---|---|
| Fable | anything except a hard, high-stakes problem the person chose Fable for | Opus, in the model menu |
| Opus | routine: drafting from a brief, reformatting, lookups, summaries | Sonnet, in the model menu |
| Sonnet or Haiku | judgment that decides something: strategy, budgets, a recommendation someone will act on, a voice that has to be right | Opus |

Suggest once per conversation, then carry on with the task. The person decides; you can't
switch it yourself. If they'd rather stay on Opus, give the effort line above instead; it
replaces the Sonnet suggestion and doesn't add to it.

## 2. Decide whether to split at all

Each subagent starts fresh and re-reads its brief and sources, so splitting has a cost of its
own. **Do the work in this conversation** when there are only a few items, when each item
needs what an earlier one found, or when the whole job fits comfortably in one pass. Split
when the items are independent and there are enough of them that one long pass would crowd
the conversation.

## 3. Route each subagent

**Set both `model` and `effort` on every dispatch.** Leave `model` out and the subagent runs
on the session's model. The dispatch tool says to set `effort` only when a skill or the
person asks for it, and this table is that ask.

| Shape of the work | Example | Model | Effort |
|---|---|---|---|
| Wide, shallow, easy to check: one fact per item | Does each of these 20 sites list a returns policy? | `haiku` | `low` |
| Collecting or extracting from sources, with a clear brief | Pull each product's pricing and features from its site | `sonnet` | `medium` |
| Drafting many items from an approved brief or template | Ad copy variations for each of 8 products from one brief | `sonnet` | `medium` |
| Research where sources need weighing | Profile each competitor's positioning and offers | `sonnet` | `high` |
| Judgment that decides something | Recommend a budget split, a strategy, a call someone will act on | `opus` | `high` |

- **Never dispatch `fable`** unless the person asks for it by name.
- **Use Sonnet as the floor for anything a person will trust without checking**: figures
  going into a report, copy going straight to a customer. Haiku is for answers that are
  cheap to check or obviously wrong when wrong.
- **Pull it together yourself.** Comparing, ranking and the final write-up stay in this
  conversation, not in another subagent.
- **Try one first on a big batch.** With 10 or more items on Haiku or Sonnet, run one or two,
  check the output, then dispatch the rest. A cheap setting that needs redoing isn't cheap.

## 4. Say the trade-off in one line

Before dispatching, tell the person what you're about to run, in plain words:

> Running 12 subagents on Sonnet at medium effort to pull each product's details. That uses
> much less of your limit than Opus. It may miss nuance on unusual pages, so I'll check them
> before the summary.

Don't put a number on the saving: prices and limits change, and a made-up figure is worse
than none.

## Where subagents aren't available

If you have no tool for dispatching subagents, skip steps 2–4 and do the work in this
conversation. Step 1 still applies. Don't mention subagents to the person.
