---
name: memory-index-distill
description: >
  Use when the auto-memory index MEMORY.md is near or over its character limit and
  its `## Always` rules need moving out of the always-on index: a "MEMORY INDEX
  NEAR CHARACTER LIMIT" session-start notice, "make this the distillation
  session", "distill the memory index", "MEMORY.md is near the limit", "run the
  index distillation", "move Always rules into rules pages". Sorts each Always
  rule by when it matters, drafts a manifest, proves it on a throwaway copy, asks
  once, then writes it. Runs in a session of its own. Do NOT use to distill one
  over-limit memory file (use memory-distill), to cut an over-limit CLAUDE.md (use
  claude-md-distill), or to hand a thread over (use handover).
---

# Memory Index Distill

Moves rules out of the always-on `## Always` section of `MEMORY.md` into pages that load only when they matter. It writes live memory and rules files **only after one approval**, and only after a proof on a throwaway copy. Each Always rule goes in one of three buckets:

| Bucket | When the rule matters | Goes to |
|---|---|---|
| `path` | while files under a glob are read or edited | `<workspace>/.claude/rules/<name>.md`, with `paths:` |
| `tool` | while a connector or API is called **through Bash**, or files in a named directory are read, from the second call on | `<workspace>/.claude/tool-rules/<name>.md`, with `tools:` |
| `index` | no trigger: writing, gates, spend, how to work with the user | stays an Always row |

**Unsure means `index`.** A rule left in the index costs a row; a rule moved to a page that never loads is silently lost.

## Steps

1. `claude-index-distill claim`. Exit 1 means another session holds the claim: report the holder and time it printed, then stop.
2. Pause the peers. Run `ListAgents`, then `SendMessage` to each local peer session with exactly: *"Memory distillation starting in another session. Finish your current unit of work, run /handover, then stop. Don't write memory until you are resumed."* Keep the list. Then run `claude-memory-lint --report`. If it shows findings, stop: `check` refuses a dirty start. Fix the findings first (ordinary memory upkeep), then resume here.
3. Read every Always row's **file**, never only its hook, and sort it: `path`, `tool`, `index`, or already carried by an existing page. A `tool` rule needs a Bash-visible trigger: a rule that matters only on MCP calls stays `index`. A rule that must govern the **first** matching call (an approval, a refusal, a check before a write, spend or send) stays `index`: a tool page arrives only after that first call has run.
4. Draft the manifest as `<UTC stamp>.json` (shape: the `## Manifest` block below) in the session scratchpad if your system prompt names one, else in `index-distill-drafts/` under the current directory. Never under `~/.claude/`: Claude Code guards it, so the write stops at a permission prompt, or fails in an unattended run.
   - Write pages one claim per bullet, under a heading that names each source slug (the `.claude/rules/engineering.md` shape).
   - A `path` page needs `paths:`. A `tool` page needs `tools:` with at least one `Bash(*...*)` pattern, and a probe of the form `python3 _probe/<name>.py <words that match the pattern>`. When the rule is about a directory outside the workspace (where no `paths:` glob reaches), also add `Read(*...*)`, `Grep(*...*)` and `Glob(*...*)` patterns, so a session that only reads there still loads the page.
   - A move's `row` is the Always line copied verbatim. Its `topic` is an existing `topics/<name>.md` that already has a Topics row (the closest one; a new topic is a manual step).
   - Every carrier a move names is in `pages[]` (`"content": null` for an existing page left unchanged).
5. Up to 4 behaviour cases, only for **discriminating** rules (default behaviour differs from the rule). Write `pass_if` first, then a `prompt` that is an incidental task with no hint of the rule, then the `files` it needs (never under `.claude/`, `mem/` or `_proof/`, and never a `CLAUDE.md`).
6. `claude-index-distill check <manifest>`. Put `null` in every `base_sha256` on the first run. `check` prints the live sha for each mismatch: copy those in and rerun until it exits 0. Then state the cost: one Haiku run per page, plus two Opus runs per behaviour case, all on the user's own Claude usage. Then run `claude-index-distill prove <manifest>`.
7. **One** `AskUserQuestion` carrying the bucket table (rule, bucket, carrier), the proof verdict and its check lines, and the step-2 peer list re-listed now with each peer's state. The first option is "Approve all — peers have stopped (Recommended)". Past about 40 rows, use grouped questions with a default. **Any change to the manifest means running `prove` again before `apply`: the proof is tied to the exact manifest bytes.**
8. `claude-index-distill apply <manifest>`, read by exit code:
   - **0** → report index chars before and after, the backup path and the restart line.
   - **4** → written, but lint findings remain or the index is still over the line; report them as the next job. (On 0 and 4, `apply` has already released the claim.) Then, **on 0 or 4 only**, tell the user each paused thread resumes in a fresh session from its handover, and that a fresh session loads the new layout.
   - **1** (refused, nothing written) or **2** (roots unresolved) → report its lines, then go to step 9.
   - **5** (failed partway and rolled back; nothing remains applied) → report it, then go to step 9.
   - **6** → **rollback incomplete**: list the paths and the backup path it printed, keep the claim (so no other session distills over a half-written tree), and stop until the user has resolved those paths.
9. On a `prove` FAIL or exit 3, the user declining, or `apply` exit 1, 2 or 5: `claude-index-distill release`, report the reason, and stop. **Never `apply` without a PASS.** Tell the user nothing was applied, and that the paused peers resume from their handovers on the old layout, unchanged.

## Manifest

Every `base_sha256` starts `null`; step 6 fills the moves' from `check`. A new page's stays `null`.

```json
{
  "version": 1,
  "pages": [
    {"file": ".claude/rules/release-notes.md", "bucket": "path",
     "content": "---\npaths:\n  - \"docs/releases/**\"\n---\n\n# Release notes — rules carried from memory\n\n## Release notes are written for users (`release-notes-for-users`)\n- Describe what changed for the user; never cite pull request or ticket numbers.\n",
     "base_sha256": null,
     "probe": {"path": "docs/releases/v2.4/notes.md"}},
    {"file": ".claude/tool-rules/github-api.md", "bucket": "tool",
     "content": "---\ntools:\n  - \"Bash(*api.github.com*)\"\n---\n\n# GitHub API — rules carried from memory\n\n## List endpoints are paginated (`github-api-paginate`)\n- Pass per_page=100 and follow the Link header: one page silently truncates a list.\n",
     "base_sha256": null,
     "probe": {"tool_name": "Bash", "tool_input": {"command": "python3 _probe/github.py api.github.com"}}}
  ],
  "moves": [
    {"memory": "release-notes-for-users.md", "page": ".claude/rules/release-notes.md",
     "topic": "topics/engineering.md",
     "row": "- [Release Notes For Users](release-notes-for-users.md) — release notes describe user-visible change, never PR or ticket numbers",
     "base_sha256": null},
    {"memory": "github-api-paginate.md", "page": ".claude/tool-rules/github-api.md",
     "topic": "topics/engineering.md",
     "row": "- [GitHub API Paginate](github-api-paginate.md) — list endpoints are paginated; follow the Link header or the list is silently cut",
     "base_sha256": null}
  ],
  "behaviour": [
    {"memory": "release-notes-for-users.md",
     "files": {"docs/releases/v2.4/changes.csv": "pr,change\n412,Search is faster on large projects\n415,Exporting an empty report no longer fails\n"},
     "prompt": "Write docs/releases/v2.4/notes.md: a short release note for users from changes.csv.",
     "pass_if": "notes.md describes both changes and cites neither pull request number (412 or 415)."}
  ]
}
```

## What the proof does and does not prove

- It shows each page loads at its trigger in a throwaway copy, and that the index lints clean under the threshold.
- It shows each behaviour case passed **once**, graded by a model.
- That is evidence, not a guarantee.
- Nothing watches tool-page delivery in real sessions after `apply`.
