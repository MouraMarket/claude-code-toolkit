# Evals — does it fire, and does it do the job

Two instruments, answering different questions. A **trigger eval** asks whether
a prompt routes to the skill. An **output eval** asks whether the skill, once
running, follows its own rules.

## Trigger eval — `claude-skill-trigger`

It runs `claude -p` against the live skill set and records **which** skill the
first `Skill` call names, killing the run there. That captures routing data, not
a yes/no. A wrong sibling and "no skill at all" are separate outcomes.

**Cases** are a JSON list:

```json
[
  {"prompt": "Write a SKILL.md for a skill that drafts release notes from a changelog.", "expect": "toolkit-maintainer:skill-builder"},
  {"prompt": "Humanize this paragraph for me.", "expect": "skill-humanizer:humanizer", "control": "positive"},
  {"prompt": "Summarize this changelog in two lines.", "forbid": "toolkit-maintainer:skill-builder"}
]
```

A case carries exactly one of `expect` or `forbid`. `"expect"` is the skill that
should win. `"forbid"` is a skill that must not: the case passes when that skill
does not win the majority of completed runs, including runs where no skill fires.
It's fully qualified (`plugin:skill`) for a plugin skill (a toolkit skill is
`<plugin>:<name>`, e.g. `toolkit-build:build`); a personal or project skill is
its bare name. `control` is omitted, `null`, or `"positive"`. Take the prompts
from the interview's trigger list. **Near-misses:** give a near-miss
`forbid: "<your skill>"`. The toolkit's `tools/build-route/trigger-cases.json`
(at `../../../../tools/build-route/trigger-cases.json` from this file) is a
worked example: `expect` cases for `/build`, and a `forbid` case per example in
its exclusion clause.

**Every run is read-only.** The tool gives each child session a hook that
blocks everything but reading, so the model in a run where no skill fires
cannot write files, run commands, call connectors, or run a skill. Your own
hooks still run, as in any session. Prompts starting with `/` are refused,
because a slash command runs before the hook can see it. Still run from a
sandbox directory, for realism, not safety: parent `CLAUDE.md` files load and
project-scope plugins resolve as in real work. Read-only changes what a run can
*finish*, not what it *routes to*.

```bash
cd <a scratch directory inside the project the skill is for>
claude-skill-trigger --cases cases.json --runs 5 --harness-control --json report.json
```

- **`--runs` (default 5).** Routing is stochastic: identical runs of one
  explicit prompt can route differently. **Every result is a count over N runs,
  never one run's verdict.** Compare rates, and don't act on a difference that
  flips between batches.
- **`--harness-control`** proves the instrument can see a `Skill` call at all.
  Always pass it. Without it, "nothing fired anywhere" can't be told apart from
  "the harness broke".
- **`--positive-control`** briefly guts a real skill's description, proves the
  route collapses, and restores it. It proves the case set *can* fail. It
  reaches toolkit-plugin skills too, resolved through the directory-source
  marketplace (the checkout the plugin loads from), and hosted plugins through
  `installed_plugins.json`. On a toolkit-plugin skill it edits a file in the
  checkout for the length of the gutted window, and restores it.
- **Record `sibling_set` from the report JSON** with any count you cite. A
  trigger eval measures the description *alongside the skills loaded on that
  machine that day*, and that set drifts whenever a plugin is installed,
  updated or removed.

**Cost depends on whether the prompts route**, because a run that fires a skill
is killed there and a run that fires nothing goes to completion. A batch where
most runs fire nothing takes several times longer and many more tokens than one
where every run routes. Token use of a killed run can't be read back afterwards:
it dies before its usage line is written. **Quote wall time, and call any token
figure a floor.** Neither counts a plugin that makes its own call per `claude -p`
session.

For a description change, run the **same cases** before and after. The delta is
the measurement.

## Output eval — `run_eval.py`

It measures whether the skill follows the rules it states in countable form.
It applies where a case file exists: `evals/cases/<skill>.json` in the toolkit
checkout, or your own file passed with `--cases`. `evals/cases/humanizer.json`
is a worked example.

```bash
cd <toolkit checkout>
git -C <skill repo> show HEAD:<path>/SKILL.md > old-SKILL.md   # before editing
python3 evals/run_eval.py <skill> --dry-run --skill-file <path>/SKILL.md --cases <cases.json>
python3 evals/run_eval.py <skill> --live --model <model id> --trials 3 --skill-file old-SKILL.md --cases <cases.json> --json > old.json
python3 evals/run_eval.py <skill> --live --model <model id> --trials 3 --skill-file <path>/SKILL.md --cases <cases.json> --json > new.json
```

For a skill in the toolkit checkout, `<skill>` alone finds both the file and its
cases; `--skill-file` and `--cases` are for a skill that lives anywhere else.

- **`--dry-run` or `--live`, one of them, always.** `--dry-run` validates the
  skill and cases and makes no API call. `--live` calls the Anthropic API with
  the key in `ANTHROPIC_API_KEY`, billed to you, and needs `--model`. Neither
  flag, no key, or no model stops before any call.
- **Cost is unknown unless you give a rate.** `--price IN OUT` (USD per million
  input and output tokens, from the current pricing page) prints a cost at
  those rates; without it the runner reports tokens and says "cost unknown".
- **`--skill-file`** grades an older revision *with today's grader*. Checking
  the old commit out instead would roll the grader back too, and the two sides
  would stop being comparable.
- **`--trials 3`** is the floor. Output is non-deterministic; one run is a
  sample.
- `--json` is a switch, not a path: it prints JSON to stdout, so redirect it.
- Compare what the skill produces, not only the pass rate: a tightened rule can
  suppress good output alongside bad and still score cleaner.
- The runner sends the skill's body as the system prompt with no tools, so
  grade only rules the body itself states. A rule that lives in a reference
  file the model never saw is not a fair check.

**A case file** is a JSON list of cases, each with a `name`, a `prompt` and a
`checks` object. Checks for text output: `must_not_match` (regexes),
`must_contain` (exact text), `no_new_numbers` (no number the prompt didn't
have), `grade_until` (grade only the text before a marker, and fail when it is
missing). Checks for list output: `min_terms`, `must_include_all`,
`must_include_any_of`, `must_not_include`, `no_duplicates`. An empty case file,
a case with no checks, or an unknown check name is refused, never passed.

## The wrong instrument

**Never measure an installed skill's triggering with `skill-creator`'s
`run_eval.py`.** It plants a fake command carrying the description and counts
every other tool call as a miss. So a prompt that correctly routes to the real
installed skill scores as a failure, and a wrong sibling looks the same as no
trigger at all.
