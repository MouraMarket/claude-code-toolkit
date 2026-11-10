# Authoring — interview, description, body, and the token inventory

## The interview (M3)

Ask only what the conversation hasn't already answered. Each answer decides
something concrete:

| Ask | It decides |
|---|---|
| The job, in one sentence | The skill's name and scope |
| 3–5 real prompts that should fire it, and 2–3 near-misses that shouldn't | The description, **and the trigger eval cases**, so keep them verbatim |
| What comes out: a file, a draft, an answer, a changed system | The output shape, and whether output eval cases can be graded |
| What it needs: tools, scripts, local files, other repos, connectors | Whether it needs bundled scripts (M4) and what it must check before relying on them |
| Who should have it | The home (Step 0): you, one project, or everyone who installs a plugin |
| How it fails: loudly, or with a confident wrong answer | How much verification it needs before shipping |

## The description

The description is the only thing that decides whether a skill fires. Get it
wrong and the skill either never triggers or collides with a sibling, and
neither failure shows until someone doesn't get the skill they needed.

When Claude decides whether a request needs a skill, it sees each installed
skill's name and description and nothing else. So the description answers one
question: **when should this skill load?** Write it to that contract:

- **Say when, not how.** Open with the situation ("Use when …"): the requests,
  moments and symptoms that call for the skill. Leave the procedure out. A
  description that outlines the steps tempts Claude to follow the outline from
  the description and never open the body, which is where the real rules are.
- **Use the words that arrive before the skill is known.** People describe the
  problem, not the skill: the error they hit, the file that grew too long, the
  thing a teammate asked for. Name those symptoms alongside the task itself.
- **Shortest that still routes.** Every clause competes for the same attention
  as every other installed skill. Cut what doesn't change when the skill fires.
- **1024 characters, hard.** The skill format caps the field, and
  `scripts/check_bundles.py` fails a longer one.
- **Name the negative space.** End with `Do NOT use for X (use Y)` wherever a
  sibling sits nearby. It's how a library of skills keeps neighbours apart.
- **Quote real phrasings.** Put the interview's trigger prompts in quotes,
  worded the way people actually type them.
- **A description competes with `CLAUDE.md` rules, not only with other
  skills.** A rule saying "read file X first" can win a prompt before any skill
  is considered. When a trigger eval shows "no skill" winning, look for the
  instruction that answered instead.
- **Different machines load different sibling sets.** Which plugins and
  personal skills are installed decides what a description competes with, so a
  collision proven on one setup doesn't transfer to another.

## The body

- **Aim `SKILL.md` at one read.** The common path goes inline; material only
  some runs need goes in `references/`. Where "the common path needs no
  reference reads" and "the file fits in one read" conflict, pick one and say
  which.
- **Write for the reader who skims.** Put the step most likely to be skipped
  where it can't be missed: in a numbered list, in bold, with its reason.
- **Give the why for any rule that isn't obvious.** A rule without its reason
  gets "optimised away" by the next editor.
- **Point, don't copy.** Where another file owns a rule, name the file and the
  section. Two copies of a rule drift, and the reader can't tell which is
  current.
- **Guard every tool the skill runs.** If a step calls a command that only some
  setups have (a `~/.local/bin` tool, a CLI, a connector), the skill checks it
  is there first and says what to install when it isn't, rather than failing
  mid-run.

## The load-bearing token inventory (any cut over ~100 lines)

A big cut loses rules silently, and it reads clean. Inventory every
backticked identifier and every multi-digit number in the old file, and
confirm each still appears somewhere in the new skill:

```bash
python3 - old-SKILL.md <path-to-skill-dir> <<'EOF'
import pathlib, re, sys
old = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
new = "\n".join(p.read_text(encoding="utf-8")
                for p in pathlib.Path(sys.argv[2]).rglob("*.md"))
tokens = set(re.findall(r"`([^`\n]+)`", old))
tokens |= set(re.findall(r"\b\d[\d,.]*\d%?", old))
missing = sorted(t for t in tokens if t not in new)
print("\n".join(missing) if missing else "all tokens present")
EOF
```

Each missing token is a question, not automatically a defect: was that rule
meant to go? The inventory can't see a prose rule that carries no number or
identifier, so pair it with one live run of the edited skill that exercises
those rules.
