# claude-code-toolkit

A Claude Code toolkit for one person's Mac: guards, hooks, subagents, skills,
permission sets, and the `install.sh` that sets them up. `CLAUDE.md` is the full
rulebook for changing it. This file is the part a reviewer needs.

`~/.local/bin` commands are **symlinks into this checkout**, and hooks, skills
and agents load from it through the profile plugin that holds them
(`toolkit-core`, `toolkit-continuity`, `toolkit-build`, `toolkit-maintainer`,
`toolkit-mac`). Optional skills ship as `skill-<name>` plugins from `plugins/`.
Editing the repo changes live behaviour on the installed machine; there is no
build step.

Build and verify:

```
./install.sh --check     # report drift without changing anything
./ci/local-checks.sh     # every check a change must pass
python3 scripts/check-capabilities.py   # registry structure; add --all --require-evidence before a release
```

**There is no `pytest`.** Each suite is a standalone script. `ci/local-checks.sh`
runs both Pythons, the secret scan, the installer, context lint, the skill
bundle limits (`scripts/check_bundles.py`) and its own self-test; it writes under `~/.cache/claude-local-ci`, so a sandbox that blocks
home-directory writes cannot run it. There, run the suites one by one. This
exits non-zero on any failing suite **and** when it finds no suites at all:

```
failed=0; ran=0
while read -r t; do
  ran=$((ran + 1))
  (cd "$(dirname "$(dirname "$t")")" && env -u CLAUDE_CONFIG_DIR -u GIT_CONFIG_GLOBAL python3 "tests/$(basename "$t")" </dev/null) || { echo "FAIL $t"; failed=1; }
done < <(find tools lib skills evals scripts ci -path '*/tests/test_*.py' -type f | sort)
echo "$ran suites run"; [ "$ran" -gt 0 ] && [ "$failed" -eq 0 ]
```

Set `PYTHONDONTWRITEBYTECODE=1` before running the leak gate's suites: a `.pyc`
embeds the absolute source path, and the tree gate flags it.

Never run `./install.sh` from a linked worktree: it repoints every symlink at a
directory that disappears when the branch merges.

---

## Code Review Rules

Each states the invariant and the safe path.

### Fail-open direction is load-bearing

The guards fail **open**: anything not positively recognised as dangerous is
allowed, because a guard that misfires on ordinary work gets disabled.
Configuration fails **strict**: a missing or broken config narrows what is
trusted. Flag any change that moves a decision the other way: a guard refusing
on absent or ambiguous evidence, or a config path that widens trust when it
fails to load. A stall is positive evidence and may refuse; a merely exhausted
time budget is not.

### A guard that cannot refuse, and a test that cannot fail

Flag an assertion, guard or fixture that passes identically whether the
behaviour it protects is present or absent, at the severity of whatever it was
meant to catch. A check must be able to reach both PASS and FAIL on a correct
machine. A `skipTest` that turns a real module into a green no-op, and a test
that depends on what happens to be installed locally, are the same defect.

### `__file__`-relative paths must resolve symlinks

Use `realpath`, not `abspath`. The commands run through `~/.local/bin` symlinks;
`abspath` lands on the wrong directory and the import fails at run time while
passing every test.

### Hooks exit 0 on unexpected input

A hook that raises costs a prompt for reasons unrelated to the work. Wrap the
body; never wrap the diagnostics.

### Writers may not guess a root path

A reader or guard may guess a path and recover. A writer may not: a wrong guessed
destination ties the write to nothing, silently. Flag code that writes to an
inferred path rather than a configured or explicit one, unless a wrong guess
fails loudly. Derive a destructive guard's exclusions from what the code just
did, never from a hand-kept parallel list.

### The shipped default is not one person's preference

`baseline` applies on every install and only narrows; a looser threshold
belongs in the user's config. Flag a change that relaxes a shipped default,
especially a safety-relevant one. Docs resolve a number from its source rather
than quoting a copy that will drift.

### Changes that reach nobody, presented as if they ship

`policy/CLAUDE.md` is imported live: an edit reaches every machine whose
`~/.claude/CLAUDE.md` carries the import, on its next `git pull`.
`CLAUDE.md.template` reaches only a `CLAUDE.md` built after the change;
`install.sh` never copies it. A decision recorded in a doc is not a
configuration change, and an open PR is not a merged one. Flag a description
that implies propagation the mechanism does not have.

### One source of truth for a live tool

A skill or command that needs a guard references `tools/<guard>/`; it never
carries its own copy. Flag a duplicated implementation of anything in `tools/`.

### Nothing ships that works only on the author's machine

No absolute paths in skills, scripts or fixtures. Bundled Python runs on 3.9.
No private names, real credentials or real project data, in code or tests:
everything synthetic. `tools/leak-gate/` fails closed (exit 0 only on complete
clean coverage, 1 on findings, 2 when coverage cannot be established) and never
prints a matched value, even inside a path or a refusal message; review it for
paths to a false 0.

### Most of this repo is instructions a model executes

A wrong sentence here is a bug no suite reads, and a model obeys what the text
says, including a contradiction between two sections or an example that breaks
its own rule. Flag an instruction that conflicts with another in the same
document, or removes a class of output without saying what survives. Resolve
every path and command from where its reader stands:

- `skills/*/*/SKILL.md`, every `SKILL.md` under `plugins/`, and `agents/*.md` run
  inside **whatever repository the user is working in**, so a bare repo-relative
  path resolves against that repository, not this one. A skill's frontmatter
  `description` decides when it fires; a wrong trigger or "do NOT use for" line
  misroutes requests with no test noticing. `scripts/check_bundles.py` checks
  size and description limits.
- `tools/build-lens/*.md`: the `/build` gate references, read through
  `${CLAUDE_PLUGIN_ROOT}`.
- `tools/codex-review/*-brief.md`: the Codex reviewer's prompts.
- `policy/CLAUDE.md`: live in every session that imports it.
- `CLAUDE.md.template`: the skeleton of a user's own `CLAUDE.md`.
- `CLAUDE.md`, `AGENTS.md` and `docs/decisions.md`: this repo's rules, the
  reviewer's rules, and the reasoning behind both.
- Strings a hook prints as `additionalContext` or `systemMessage`, which go
  straight into a live session.

Apply the `instruction-docs` lens (`tools/build-lens/code-review.md`; the
"Instruction documents" check in `tools/codex-review/review-brief.md`) to all of
them.
