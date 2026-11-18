# Run brief template

Copy into `<specs_dir>/YYYY-MM-DD-<subject>-architecture-<moment>-brief.md` and fill every
`<…>`. This is the only thing besides the fixed instructions that reaches the
reviewer, so it carries **facts and questions, never conclusions**: no prior
review's findings, no "we think X is broken", no summary of what changed since
a previous round.

```markdown
---
status: live
subject: <subject>
---

# Architecture review brief — <build name>

moment: <design | final>

## The objectives — verbatim

<The user's own words, quoted exactly, with date and source (mission spec
`## MVP`, a message, a ruling). Several statements → number them.>

## The objectives — parsed

<prompt-optimizer's parse: each objective as one testable line, plus the success
criteria and constraints it implies. Where the parse goes beyond the words, say
so.>

## What to read

| Surface | Path | Note |
|---|---|---|
| <the design / the shipped system> | <absolute path> | <what it is> |
| Mission spec | <absolute path> | `## MVP`, `## Backlog`, decisions |
| <requirements / plan / decisions file> | <absolute path> | |

Repository: `<absolute path>` @ `<commit sha>`.

## Decisions already made

<Decisions of record the reviewer should treat as settled unless the build gives
new evidence against them — each with its date and where it is recorded.>

## Out of scope

<What not to review, and why — e.g. bugs already gated, a deferred area.>

## Questions the user wants answered

<Suspicions phrased as open questions, never as findings: "Does X hold up at ten
clients?", not "X does not scale".>
```
