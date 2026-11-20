---
name: skill-builder
description: >
  Use when creating, editing, reviewing or testing a Claude Code skill — "build
  a new skill", "write a SKILL.md", "turn the workflow we just did into a
  skill", "my skill isn't triggering, rewrite the description", "review this
  skill before I ship it", "set up trigger evals for this skill", "this SKILL.md
  is too long, split it". The skill-authoring process, used instead of
  skill-creator: it covers where a skill lives, description writing, the size
  budget, trigger and output evals, shipping by where the skill lives, and when
  skill work is big enough to hand to /build. Do NOT use to run or invoke an
  existing skill, for general prompt writing, or for a multi-file build that
  isn't about a skill (use /build).
---

# Skill Builder

The authoring process for Claude Code skills. It routes a request by the size
of the edit, and each route names the checks that edit needs. It is a
`toolkit-maintainer` plugin skill that runs shell tools from your toolkit
checkout (`claude plugin marketplace list` shows its path for
`claude-code-toolkit`).

It **points at** canonical sources rather than restating them. Description
craft lives in `references/authoring.md`; read it where this file says to.

## Step 0 — where the skill lives

Settle this first on a new skill, or whenever the home is unclear.

| The skill is… | Home | Ships by |
|---|---|---|
| For you, in every project | `~/.claude/skills/<skill>/SKILL.md` | Nothing: a new session loads it |
| For one project | `<project>/.claude/skills/<skill>/SKILL.md` | Committing it in that project |
| Shared through a plugin you maintain | `<plugin repo>/<plugin>/skills/<skill>/SKILL.md` | A plugin version bump and an update of the installed copy |
| Tooling for this toolkit's own hooks and commands | `skills/<profile>/<skill>/` in the toolkit checkout, shipped in that profile's plugin | The toolkit's suite and catalogue (Ship, below) |

Pick the narrowest home that reaches everyone who needs the skill. Two skills
with the same bare name in different homes make it ambiguous which one runs.

## Step 1 — pick the mode

Mechanical, not judgment. Check in this order, **first match wins**:

| Mode | Matches when | Then |
|---|---|---|
| **M4** | Bundled scripts or tools, a skill family that moves together, or ≥3 files of *new* content (moving existing text into `references/` doesn't count) | Hand off to `/build` and stop here. |
| **M3** | The skill doesn't exist yet | Step 2, M3 |
| **M0** | Only the frontmatter `description` changes | Step 2, M0 |
| **M1** | A body edit in one file, description unchanged, not a restructure | Step 2, M1 |
| **M2** | Anything else on an existing skill: a restructure, a split into `references/`, a new capability, or description and body changing together | Step 2, M2 |

**When unsure between two modes, take the heavier one.** Complexity found
mid-edit moves you up a mode, never down.

Why M4 leaves: `/build` exists for multi-file work with real verification, and
its own "When NOT to use this" section keeps one-file work out. M0–M3 are
one-file work. They stay here.

## Step 2 — what each mode requires

**M0 — description only**
1. Trigger eval (`claude-skill-trigger`) on the old description, then the new
   one, **same cases** (`references/evals.md`). A description change without
   one is a guess: the description is the only thing that decides whether a
   skill fires.
2. Ship (below), by the skill's home.

**M1 — small body edit**
1. Size check (Size budget, below).
2. If an output eval case file exists for the skill (`evals/cases/<skill>.json`
   in the toolkit checkout, or your own, passed with `--cases`), snapshot the
   old file before editing (`git show HEAD:<path>/SKILL.md > old-SKILL.md`) and
   run the output eval A/B against it (`references/evals.md`). A one-line rule
   change is exactly what those cases exist to catch.
3. Ship (below), by the skill's home.

**M2 — restructure, split, or new capability**
1. **Call `skill-prompt-optimizer:prompt-optimizer` explicitly with the `Skill`
   tool**, unless it already ran this turn. Never leave it to trigger on its
   own: routing is stochastic, and a step left to self-trigger is a step that
   gets skipped. The maintainer profile installs it as a dependency.
2. Snapshot the old file first: `git show HEAD:<path>/SKILL.md > old-SKILL.md`.
3. Edit, then run the size check.
4. If you cut more than ~100 lines, run the load-bearing token inventory
   (`references/authoring.md`). A cut's real risk is a rule silently lost.
5. If an output eval case file exists for the skill, run the output eval A/B
   against the snapshot (`references/evals.md`).
6. If the description moved, run the trigger eval, same as M0.
7. Ship (below), by the skill's home.

**M3 — new skill**
1. **Call `skill-prompt-optimizer:prompt-optimizer` explicitly**, as in M2.
2. Step 0: the home.
3. The interview, then the draft (`references/authoring.md`).
4. Size check.
5. Eval cases: trigger cases from the interview's prompts at minimum, and output
   cases where the skill's rules are countable (`references/evals.md`).
6. Trigger eval.
7. Ship, including the new-skill steps.

M0 and M1 never call `prompt-optimizer`. They're the narrow edits its own
description says to skip.

## Size budget

| Bound | Value | Where it comes from |
|---|---|---|
| `SKILL.md` body | **under 500 lines** | Anthropic's skill authoring guidance |
| `description` | **1024 characters** | The skill format's limit on the field |

Check with `python3 scripts/check_bundles.py <skill dir>` from the toolkit
checkout: over 500 lines fails, and so does a description over 1024 characters
or one it cannot read. Both ends of the curve cost round trips: a `SKILL.md`
that's too big gets skimmed, and too many `references/` on the common path cost
one read each. Put the common path in `SKILL.md` and rare material in
`references/`.

**500 lines is a hard cap, with no exception list: trim, never pin.** Trim by
moving and compressing, never by cutting a verified rule: move rare material
into `references/`, merge duplicated tables, tighten prose.

A long file's usual failure is being **skimmed**, not truncated. A step that
gets skimmed past is never performed, and nothing reports it. So judge a draft
by which steps are at risk of being skipped, not by its length alone.

## Ship — by the skill's home

Ship the way the Step 0 home ships. Each home has its own checks, and running
another home's checks validates nothing.

**Every home:** run `python3 scripts/check_bundles.py <skill dir>` from the
toolkit checkout and read its count line. It prints how many skills it checked
and refuses when it finds none, so a wrong path cannot pass. Then the trigger
eval, if the description is new or moved.

**A skill other people install is review-gated.** The toolkit policy's
silent-failure rule covers anything installed onto another machine. Run that
review **before opening the PR**, and record the call in the PR body. A skill
that drops a rule doesn't crash, it just stops doing the thing, and nobody
downstream notices.

**Personal:** nothing more. A new session picks it up.

**Project:** commit it under the project's own review.

**A plugin you maintain:**

1. Bump `version` in that plugin's `.claude-plugin/plugin.json`: patch for copy
   edits, minor for a new skill or capability, major for a removal or rename.
   Installed copies are cached per version, so an unchanged version can leave
   everyone on the old skill with no error.
2. **A merged skill is not live in your own Claude Code yet.** Run
   `claude plugin marketplace update <marketplace>`, then
   `claude plugin update <plugin>@<marketplace> --scope user`, and restart.

**`claude-code-toolkit`:**

1. Run the skill's own suite if it has one (`skills/<group>/<name>/tests/`), then
   `./install.sh --check` for the whole suite.
2. On a new skill: create it under `skills/<profile>/<name>/SKILL.md` with `name: <name>`,
   in the group of the profile that should carry it; its group's plugin ships it (each profile is one plugin in the `claude-code-toolkit` marketplace), with
   no link. No installer change. `./install.sh --check` warns if its bare name clashes with a third-party
   plugin (the user's call), and fails if it clashes with a personal copy or another
   toolkit profile, or if the plugin inventory cannot be read in full. Live sessions pick it up after `/reload-plugins`.
3. Run `python3 tools/catalogue/build_catalogue.py` and commit `docs/catalogue.md`;
   the catalogue suite fails on a stale file. Add the skill's entry to
   `docs/capabilities.json`; `lib/tests/test_capabilities.py` fails on a skill
   with no entry.

## When to read the references

- `references/authoring.md`: on M3, on any description change, and before any
  cut over ~100 lines. It covers the interview, the description contract, body
  writing, and the token inventory.
- `references/evals.md`: on M0, on M1 and M2 when eval cases exist (and on M2
  when the description moved), and on M3. It covers the trigger eval, the
  output eval, and what their numbers can and can't claim.
