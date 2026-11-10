# Plan Review requirements

This is the fixed rubric a Plan Review judges a plan against. It is
identical for every plan reviewed through `claude-merge-brief --plan`, and
it names no specific plan, PR, revision, or finding — only what any plan
must satisfy before its first task is implemented.

## Structure

- Every task states explicit, checkable acceptance criteria and an exact
  verification command — one an executor can run without inventing a path,
  a name, or a fixture the plan never specified.
- A verification command must be CAPABLE of failing if the task were
  implemented wrong. A command that only checks an exit code against code
  that swallows its own errors, or that would pass unmodified against the
  repo before the task ever ran, verifies nothing.
- Tasks are ordered so no task depends on a file, function, config key, or
  fixture that a LATER task creates.
- A task producing a name — a function, field, config key, or path — that a
  later task consumes declares it explicitly; a task consuming such a name
  states which earlier task produced it. An executor sees only its own task,
  so a name agreed nowhere is a name it invents.
- A function, field, or path named in one task is spelled identically in
  every other task that references it.
- The plan names every file it creates or modifies, with each file's
  responsibility, before its first task.
- Where a task produces code with a test, the plan orders writing the failing
  test and observing it fail before the implementation step.
- Work the plan labels `Classification: bounded` that adds a new subsystem,
  or restructures how existing components fit together, is architectural:
  it needs an approved design, and the bounded label is a finding.

## Coverage

- When the brief carries a `## Source requirements` section, that is the
  source; check every requirement in it against the tasks.
- The plan delivers everything the source it answers to (a spec, a mission,
  a prior review) asked for. Anything the plan quietly drops, defers, or
  narrows must say so explicitly — an omission stated nowhere is a defect,
  not a scoping choice.
- Nothing in the plan contradicts itself: a design decision made in one
  place and reversed, unstated, in another is the same class of defect as
  an omission.

## Ambiguity

- No task leaves a name, path, schema, interface, or a behaviour on empty or
  malformed input for the executor to invent. Two readings of a task that
  would produce materially different code is a defect, whether or not
  either reading is "reasonable."
- Any claim a plan makes about existing code, tests, or behaviour is
  checked against that code, not asserted on the plan author's authority
  alone.

## House rules

- The plan does not violate the target repository's own `CLAUDE.md`,
  including its nearest nested one — especially anything recorded under a
  "decisions not to re-litigate" heading. Quote the rule a task conflicts
  with, not just the conflict.

Judge the plan text that follows against every rule above. A rule the plan
does not violate needs no comment; a rule it does violate is a finding.
