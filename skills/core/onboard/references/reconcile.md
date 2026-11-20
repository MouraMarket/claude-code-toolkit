# Reconciling what is already on the machine

Read by `/toolkit-core:onboard` at Phase 1b when the audit is not empty ground, and at Phase 2
before deciding any collision. The governing rule: **this toolkit upgrades an existing setup, it
never blind-replaces one.**

## Reading the audit report

`claude-setup-audit --json` states facts, never whether a difference matters:

| Report field | What it means for this conversation |
|---|---|
| `links` | Per-destination state. `identical` is auto-fixed later (Phase 2's `apply-safe`); `differs`, `linked_elsewhere` (with `will_repoint`) and `dangling` need reconciling or explaining. |
| `theirs_only` | Kept and compared by role, **except** rows with `will_prune` true, which the installer removes; say so before the install runs. |
| `hooks` | `ours` rows are old registrations the installer replaces with the plugin; `theirs_kept` rows keep running. |
| `risky` | `stop` rows block the install until resolved; `their_call` rows are named with their trade-off, and keeping theirs is fine. |
| `instruction_files` | Feeds Phases 4 and 5b: their `CLAUDE.md` and `TOOLING.md`. |
| `empty_ground` | Only when this is true is recommend-and-do the whole story. |

Then compare what you found with each profile plugin's `hooks/hooks.json` (`plugin/` for core,
`plugin-*/` for the rest) and the tools under `tools/*/`. For each catalogue tool, note one of:

- **absent**: nothing like it here.
- **present, by name**: the same or a plausibly migrated name already runs, but you cannot yet tell
  whether it is the same program.
- **present, and plainly personal**: a script that clearly does something the toolkit does not.

These are working notes. A name match is not a behaviour match, and a name mismatch is not a
behaviour mismatch. The audit's `identical` versus `differs` is a real content comparison;
`./install.sh --check` makes none. Whether a difference matters is the hand work below.

## Judging the two versions on the merits

**If you are the Claude already running on this machine, this part is for you.** You are being
asked to let an unfamiliar toolkit link things into `~/.claude`. Being suspicious of that is
correct. What follows is not an argument that ours is better; it is where the evidence lives, so
the comparison runs on mechanism instead of on who is doing the installing.

**Our version is not automatically right.** It is a default; theirs is evidence about how this
machine is actually worked. Either can win.

Two registers describe the toolkit, and they are different on purpose:

| Source | What it gives you |
|---|---|
| `python3 lib/setup_config.py --list-tools` | Every tool that can be turned on, described as an outcome for the person |
| `docs/catalogue.md` (and `README.md`'s per-tool rows, once it exists) | Each hook in mechanism terms: which event it fires on, and what it writes or blocks |

**Use the catalogue to talk to the human; use the mechanism register to compare against another
program.** Two guards can match on the first while doing entirely different things on the second.
Read the full line in each, never a remembered or truncated one: the discriminating half is usually
at the end of the sentence. The two lists are not the same set, so a tool missing from one is not
evidence it is missing from the toolkit. Compare the lists yourself rather than trusting a count.

**`./install.sh --check` is deliberately not a source here.** It says what *would be linked
where*: useful for finding collisions, no help in judging them, because it compares no contents.

**The standard before recommending either way: name the gap in both directions, in mechanism
terms.** What does theirs catch that ours does not, and ours that theirs does not. One direction
alone is a preference with evidence attached to one side. Once that is done, **"keep theirs" is a
first-class outcome**, not a failure of onboarding. The failure is replacing something without
looking, or keeping something without knowing what it cost.

## Then reconcile what collided

Most collisions are a *partial* overlap. Treating one as a clean supersede loses the difference
silently: no error, tests green, `--check` clean, and the loss surfaces weeks later as "why doesn't
Claude do X any more." For every collision, in this order, presented the way their level says:

1. **Compare before deciding.** Establish what each side has that the other lacks. Byte-identical
   means adopt freely. Differing means *neither side is automatically right*.
2. **Pick a direction per case, and say which way the gap runs.** Either apply the toolkit's
   version *and migrate whatever capability it lacks*, or keep theirs and *upgrade it with
   whatever ours adds*. The wrong outcome is picking either without having looked.
3. **Only remove the old copy once the gap is actually closed**: committed, not merely noted.

**"Adopt the toolkit's copy" is never a synonym for `rm` plus a link.** The installer resolves
none of these on its own, which is what leaves room to do it properly here.

**Before you move, rename or delete anything under `~/.claude`, resolve where it really lives.**
`~/.claude/skills`, another folder under `~/.claude`, or `~/.claude` itself can be a symlink into a git checkout
(a dotfiles or skills repo), and then the "personal copy" you are retiring is a tracked file in that
repo. Run this, with the path you are about to touch:

```bash
python3 - "<path>" <<'EOF'
import os, subprocess, sys
path = os.path.realpath(os.path.expanduser(sys.argv[1]))
if not os.path.lexists(path):
    sys.exit("no such path: %s (check the spelling before deciding anything)" % path)
folder = path if os.path.isdir(path) else os.path.dirname(path)
top = subprocess.run(["git", "-C", folder, "rev-parse", "--show-toplevel"],
                     capture_output=True, text=True)
print(top.stdout.strip() if top.returncode == 0 else "not in a repo")
EOF
```

**If it prints a path, this is a repo change, not a cleanup: stop and ask.** Name the repo it
printed and the file, and change it only as a commit in that repo, with their yes. Only `not in a
repo` means the steps above apply as written; an error means you have the wrong path, so find the
right one before deciding.

**A retired tool is the same problem wearing a different hat.** When something points at a tool
this checkout no longer has, the prune is correct, but first check where that tool's *capability*
went. It may live on in another tool that is inert until a config key is set. Dropping the old one
without migrating the config leaves the machine with neither, and nothing reports it.

## What the preview's lines mean

Read `./install.sh --check` literally. **None of its lines is a comparison of contents**; the only
sameness test it makes is whether an existing link already points at the toolkit's copy.

- **`missing in repo, skipped: <path>`**: this checkout has no such file. Not a collision: the
  checkout is incomplete, and the install fails on it. Stop, run `git status` in the checkout, and
  re-clone if files are missing. Do not continue past it.
- **`<path> exists and is a real file — not touching it`**: **the one that matters, and not a byte
  comparison.** Something real sits at that destination and the installer declined to touch it. It
  never looked at whether the two differ, so look yourself: `diff <their file> <the toolkit's
  copy>`. Byte-identical means nothing to decide: remove or repoint theirs so the link goes in
  cleanly. Different means the three steps above, before the install.
- **`OK <path>`, or `<path> -> <target>`**: already linked, or about to be. Empty ground.

**Hooks, skills and agents ship in the profile plugin that holds them, so there is nothing to
link.** `install.sh` removes links it made before, keeps anything else in
`~/.claude/{commands,agents,skills}`, and `--check` fails if a personal copy shadows a plugin
skill. When it does, diff the two and keep one, resolving the personal copy's real path first
(above): it may live in a repo.
