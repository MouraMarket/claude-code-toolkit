---
name: onboard
description: Set up this Mac's Claude Code with this toolkit. Detects what is already on the machine, recommends rather than interviews, installs the profiles the person chooses, and proves the safety guard works. Run it again with `verify` in a fresh session after the restart.
argument-hint: [verify]
---

Onboard this person onto this toolkit's Claude Code setup.

**If `$ARGUMENTS` is `verify`, skip straight to Phase 6b and do nothing else.** That is the second
half of onboarding, run as `/toolkit-core:onboard verify` in a fresh session after the restart.

**Until they tell you otherwise, assume they are not technical** (*Their level*, below, lets them
say otherwise). **And assume the machine already has a setup that works for them**: a `CLAUDE.md`
someone tuned, scripts that run automatically, permission rules they trust. **You are upgrading a
working system, not installing onto a blank one.** So: **look before you touch, compare before you
decide, and merge rather than replace.** Neither version is automatically right: ours is a good
default, theirs is evidence about how they work. End with both sides' strengths, and say which way
each gap ran. **This does not license silent judgement calls**: name each collision and its
trade-off in plain terms, then act.

If their Claude is suspicious of what we are about to install: good, that is the correct posture.
`references/reconcile.md` is written for that reader.

## How to behave

**Recommend and do; don't interview.** For anything with a right answer, say what you're setting up
and why in one sentence, then do it. Ask only where *they* are the only possible source of truth:

1. How much of this is new to them: *Their level*, below, before anything else.
2. Their first name.
3. What they do: their role and what they own.
4. How they want you to talk to them.
5. Where they keep their work (Phase 1).

Everything else is a recommendation you make and apply, never a choice they cannot evaluate.

**"Recommend and do" governs empty ground, not a collision.** Where they already have a file, only
they know why it exists. So where something is in the way, stop, show the difference, and let them
decide. **A collision is never resolved by your preference alone, and never resolved silently.**

**The deliberate exception is the consent prompts in Phase 2** (profiles, tool groups; the Workflow
setting has no recommended answer): permission nobody else can give, so the recommended answer is one
keypress. **Detect first, then ask:** Phase 1b looks silently, so each question names the stakes.

**Plain language, always**: translate a term the first time ("a hook is a small script Claude runs
automatically"). **Never show a wall of terminal output**: report the outcome in a sentence.

**Never apply the autonomous permission grant.** `./install.sh --autonomous` and
`skills/core/autonomy-permissions/` are advanced opt-ins for later, and wrong for someone new. If
they ask, say it exists, say it's for later, move on. Tell them at the start what will happen and
that it takes one to three hours, longest over an older install, much of it one unattended test run. Never promise less.

## Their level — ask this first

Before Phase 0, ask one `AskUserQuestion`. It sets how you explain everything after it:

- **New to this** *(recommended if unsure)*: hasn't used a terminal on purpose, or Claude Code.
- **Some experience**: has used Claude Code or a terminal, but hasn't built a setup of their own.
- **Power user**: runs their own Claude Code setup and knows what a hook, a settings file and a symlink are.

Take the answer at face value. "More detail, please" moves them down a level; "skip the
explanations" moves them up one.

**The level changes how you explain and how collisions are walked. Nothing else.**

| | New to this | Some experience | Power user |
|---|---|---|---|
| **Terms** | Translate every one, the first time | Translate only the toolkit's own (hook, guard, worktree) | No glosses |
| **Tier 1 and 2 tools (Phase 2)** | Two or three sentences each | One sentence each | The `--list-tools` line as it stands |
| **A collision (Phase 2)** | One at a time, in outcome terms, with your recommendation | A short summary per collision with your recommendation, the diff offered | The audit's rows as a table with the diff paths, decided in one batch (`AskUserQuestion` caps options per call, so a long list takes several) |
| **Progress** | One plain sentence per step | One sentence per step | Only what failed or needs a decision |

**Identical at every level:** the snapshot; `apply-safe` touching only byte-identical files; every
consent prompt in Phase 2; the rule that the machine never ends up unguarded; the danger-guard check
in Phase 6b; and the autonomous grant, never offered. A power user saying "just install everything"
gets the consent prompts in their fastest form, one keypress each, never skipped. A batch of
collision decisions is still *their* decision, made with the gap named in both directions.

Write the level into their `CLAUDE.md` in Phase 4, so later sessions pitch at the same level.

## Phase 0 — Check the basics

You should be inside the toolkit checkout they cloned (`git remote -v`). Check the prerequisites in
one batch and read the results yourself:

```bash
claude --version; python3 --version; git --version; uname -s; pwd; command -v gh && gh auth status 2>&1 | head -3
```

- **`uname -s` is not `Darwin`** → tell them plainly this toolkit is macOS-only, and stop here. Nothing has changed yet.
- **`python3` missing** → stop. `xcode-select --install` is the usual fix; offer to run it.
- **`gh` is optional.** GitHub's CLI is needed only for pull-request and status features. If it is missing or signed out, say so in one line and carry on.

### Snapshot before anything changes

A copy of their current Claude setup is saved first, so anything onboarding changes can be put back.
On exit 2 or 3: stop, change nothing, and get help.

```bash
python3 tools/setup-audit/claude-setup-audit snapshot
```

## Phase 1 — Where their work lives, then configure

First look for an existing toolkit configuration:

```bash
python3 -c 'import json, os; p = os.path.expanduser("~/.config/claude-code-toolkit/config.json"); print(json.load(open(p)).get("workspace_root") if os.path.exists(p) else "NO CONFIG")'
```

**A configuration already exists** (it printed anything but `NO CONFIG`): never replace it, and
never run the `--write` block below. If it printed a path, tell them the work folder it names and
confirm it. If they want a different one, onboarding keeps the current folder:
`setup_config.py --update` deliberately refuses `--workspace-root`, because the guard's protected
paths derive from it, and changing it is not supported yet. Go to Phase 1b. If it printed `None`,
the file names no work folder and nothing here can add one safely: stop and get help.

**Otherwise** (`NO CONFIG`), **ask where they keep their work**: the one layout question, which
only they can answer. Offer a guess from where they cloned this checkout:

```bash
python3 -c 'import sys; sys.path.insert(0, "lib"); import setup_config; print(setup_config.guess_workspace_root(sys.argv[1]) or "")' "$(pwd)"
```

One `AskUserQuestion`: the guess (when there is one), a folder they name, or a new folder they ask
you to create. **Recommend only the printed guess, never a folder of your own choosing.** An empty
guess means no recommendation: the guess deliberately skips a folder holding nothing but this
checkout, since that folder is the toolkit's, not their work. Say why not Documents, Desktop or a cloud-synced folder (iCloud Drive, Google Drive,
Dropbox, OneDrive): a folder that syncs is a bad place for files Claude may delete or rewrite.
Create one only when they asked for a new folder:

```bash
mkdir -p "<the folder they chose>"
```

If this checkout sits in Downloads or on the Desktop, suggest moving it into their work folder with
`mv` first (never required), then `cd` there and continue.

**No configuration yet**, so write it now; their first name is the only other answer it needs:

```bash
python3 lib/setup_config.py ~/.config/claude-code-toolkit/config.json "$(pwd)" \
  --write --name "NAME" --workspace-root "<the folder they chose>"
```

Tell them: "Claude now treats that folder as your work folder: inside it, it can create and delete
files; outside it, the usual delete commands (like `rm -rf`) get blocked. That catches common
mistakes; it is not a wall around your files." **If it exits 3**, stderr says why the folder
was rejected: fix what you can and involve them only for a real decision. Never hand-write the
config to get past the check; the check is the safety feature. Onboarding sets no folder for plans
and specs: the tools that keep them ask the first time they need one.

## Phase 1b — Detect what's already here, silently

Before anything is offered, look at what Claude setup is already running. **Nothing is installed or
changed here**, and nothing is shown as raw output.

```bash
python3 tools/setup-audit/claude-setup-audit --json
```

It states facts, never whether a difference matters. When `empty_ground` is true, recommend-and-do
is the whole story and there is nothing to reconcile. Otherwise read the report with
`references/reconcile.md` ("Reading the audit report") and keep working notes per tool. Act on none
of it yet: the audit settles byte-sameness only, and whether a difference matters is Phase 2's
reconciliation.

## Phase 2 — Turn things on, then install

**Decide what to turn on before running the installer**; it reads the answers.

```bash
python3 lib/setup_config.py --list-tools
```

Read it yourself: everything that can be turned on, in plain language, grouped the way you will ask.
Take explanations from it, never from memory. `docs/catalogue.md` says what each profile brings.

**Tier 1 — explain it, don't offer it.** At their level's length, say what the safety checks do:
block genuinely dangerous commands and keep Claude working in the right copy of a project. They are
always on: they make it safe to let Claude work unprompted. **Secret scanning on commits is not
one of them:** it is off until `setup-git.sh` runs in Phase 3, so never call it on before then.

**A collision is a comparison, not a veto.** If Phase 1b found their *own* guard for the same job,
settle "which protects you better?" in the reconciliation step, gap named both ways; keeping theirs
is legitimate. **Running with no guard at all is never available**: if a comparison would leave
nothing covering dangerous commands, say so and treat it as empty ground.

### Profiles — ask this before any tool question

Read each profile's `summary` and `default` from `claude/profiles.json` rather than restating them.
Core (the safety checks and this onboarding) is always installed. Then one `AskUserQuestion`,
`multiSelect: true`, each option described from its `summary`:

- **continuity**, recommended because its `default` is true. They can decline it; say what it brings first.
- **builder**
- **maintainer**: also brings builder and installs the `skill-prompt-optimizer` plugin (its `docs/catalogue.md` "Also installs" line); say both, and that it is for people who maintain this toolkit.
- **Mac**: only when `uname -s` printed `Darwin` in Phase 0. Add the `caffeinate` explanation from `--list-tools`; choosing Mac turns it on.

Write `core` plus their choices. This is the recommended answer:

```bash
python3 lib/setup_config.py ~/.config/claude-code-toolkit/config.json "$(pwd)" \
  --update --profiles core,continuity,builder
```

Change it to match. Keep `continuity` unless they declined it, because a list replaces the default
set; if they declined it, say in words that it is left out on purpose. Re-run `--list-tools`:
`profile_selected` now follows the list.

**The Workflow setting, builder only.** Ask only when they chose builder and either their config
already has `build.enable_workflows: true`, or this session has no `Workflow` tool, neither loaded
nor in your deferred-tool list. Say it is one Claude Code setting for every session; `/build` works
without it; `install.sh` writes, records and removes it. With no Workflow tool, offer *turn it on*,
or *leave it* (nothing written), **tag neither** (no "Recommended", nothing pre-selected), and never
as one tab of a multi-question form: a default submitted unread is a choice they never made.

```bash
python3 lib/setup_config.py ~/.config/claude-code-toolkit/config.json "$(pwd)" \
  --update --set build.enable_workflows=true
```

When the config already turns it on, offer *keep it* (nothing written) or *turn it off*: the same
command with `build.enable_workflows=false`; the install then removes the value only if the toolkit
wrote it and it still reads `true`. That `=false` plus `./install.sh` is the undo at any later time.
Never edit `settings.json` yourself.

**Tier 2 — one question per group, all in the same batch.** For each tier-2 group `--list-tools`
returns, ask one `AskUserQuestion`: *turn all on (recommended)* / *let me pick individually* / *turn
all off*. One single-select per group, because `AskUserQuestion` takes at most 4 options and cannot
pre-check one: a multi-select of everything would silently turn off whatever they didn't tick. On
"pick individually", follow up with a multi-select of that group's tools.

**Skip any tier-2 tool whose `present` or `profile_selected` field is false**, without asking: its
profile was just chosen or declined. If they ask about one, name the profile that brings it (its
`profile` field).

**Tier 3 has no question of its own.** The Mac answer is the `caffeinate` question: choosing Mac
turned it on, declining turned it off. Never pass `caffeinate` to `--enable`. Then, without offering
it, say autonomous permissions exist, what they do, and that they are left for later.

Write what they chose, each tool's `section` comma-separated, dropping an empty flag. If no tier-2
tool was offered, skip this. Never hand-write the config instead; on exit 3, read stderr, fix it,
and re-run.

```bash
python3 lib/setup_config.py ~/.config/claude-code-toolkit/config.json "$(pwd)" \
  --update --enable <sections they accepted> --disable <sections they declined>
```

### Preview first — this changes no setup

First run `python3 lib/checkout_fingerprint.py` and keep the hash it prints: one hash naming the content of every file the tests could see. Then:

```bash
./install.sh --check
```

It changes no setup (no link swapped, no setting written), but it runs the whole test suite, so
**raise your command timeout generously**. **A killed run looks finished**: the report prints in
seconds and the `Tests` section runs last. Trust it only when it ends with `Check complete. Re-run
without --check to apply.`, or exits 1 after a summary (`Some suites failed`, `Hooks are NOT
protected`, `Required components failed:`). With neither, it was cut off: re-run with a longer
timeout. Keep the output. If a suite fails, stop **here**, before anything changed, and get help rather than debugging it with them. **This is onboarding's only full test run:** once it passes, run `python3 lib/checkout_fingerprint.py` again. The same hash as before the preview is the record: the checkout the tests passed on. A different hash, or an error, means the checkout changed during the run, so run the preview again. Each suite runs in its own empty home folder against the checkout's files, so nothing the install changes on their machine can change a result; only a change to the checkout can.

**Act on two kinds of preview line before installing:**

- `profile <p> is no longer selected (would uninstall)`: a profile the last install chose and this selection drops; the install will uninstall it. Ask: *let it go*, or *keep* it. To keep it, re-run the `--profiles` command above with it added (maintainer brings builder, so keep both), then the preview.
- `<plugin> enabled outside your profiles — left as is`: installed by hand; the installer never touches it, and its hooks still run. Ask: keep it, or turn it off with `claude plugin disable <plugin>@claude-code-toolkit`, maintainer before builder, because maintainer depends on builder.

### Reconcile what collided

Run this first. It removes only files byte-identical to the toolkit's copy, after saving another
snapshot:

```bash
python3 tools/setup-audit/claude-setup-audit apply-safe
```

For anything else Phase 1b or the preview named, **read `references/reconcile.md` before deciding**,
and follow it: compare before deciding, name the gap both ways, keep theirs when theirs is better,
remove an old copy only once its gap is closed. Present each collision as their level says. **Expect
to be here**: assume work until the preview shows none. Move on once anything worth keeping is
committed, not just noted.

### Now install

First take the fingerprint again (`python3 lib/checkout_fingerprint.py`) and compare it with the record. **Identical:** install without the tests, which the preview ran:

```bash
CLAUDE_INSTALL_SKIP_SUITES=1 ./install.sh
```

Its `Tests SKIPPED (CLAUDE_INSTALL_SKIP_SUITES=1) — not a pass` line is expected. **Any difference, or an error** (reconciling changed a file in the toolkit, a pull, another branch): run plain `./install.sh`, which tests the checkout again. Once it passes, its fingerprint is the new record. Either way it installs and turns on the plugin for each profile they chose, turns on what was agreed, and applies the baseline safety rules; report the outcome in one line. If it says `~/.local/bin is not on your PATH`, offer to add it to their shell profile (one optional command won't be callable by name; nothing else is affected). If a suite fails, stop.

## Phase 3 — Git hooks and recommended plugins

Say what these do **before running them**: the first sets sensible git defaults and a commit check that blocks credentials; the second installs a few optional plugins from Anthropic's official marketplace.

**Name the machine-wide part first.** `setup-git.sh` sets git's `core.hooksPath`, which applies to
*every* repository on this machine and replaces per-repository hooks (ours forward each client-side
hook to the repository's own). Secret scanning on commits is not active until it has run. A change
with that reach is theirs to accept knowingly; an existing pre-commit setup of theirs is left alone.

```bash
./scripts/setup-git.sh
./scripts/setup-plugins.sh
```

Report outcomes, not logs; a plugin that did not install gets one line.

## Phase 4 — Their CLAUDE.md

This makes Claude useful to them, and it is where you need their input. `<workspace_root>` is the
work folder from Phase 1:

```bash
cp -n CLAUDE.md.template "<workspace_root>/CLAUDE.md"   # -n: never overwrite
```

**Run it; never hand-write the file**, even into an empty folder. **`-n` is load-bearing, and not
the whole fix**: a bare `cp` destroys a real file on a re-run; a silent skip loses every template
improvement. **If it skipped, the file is theirs:** port a personal section (About me, Tone, My
machine) only if theirs lacks it; old toolkit-policy copies are Phase 6b step 6's `legacy-copy`.
Explain it in one sentence: a file Claude reads every time, so they don't repeat themselves. Toolkit
policy loads separately, is replaced when the toolkit updates, and their file overrides only its
defaults. **You fill it in**, working through the `TODO` markers:

- **About me.** Ask what they do, what they own end-to-end, and what parts of this are new to them (people under-report that, so ask directly). Draft two or three sentences and show them. **Include the level they chose at the start, in plain words** ("new to Claude Code and the terminal: explain steps as we go"), since later sessions read it.
- **Tone.** Ask, with concrete options: concise and businesslike; warm and direct with some humour; or thorough, with the reasoning spelled out.
- **My machine.** Do **not** ask. Read it (`uname -sr`, `sw_vers`, whether VS Code is installed) and write it, including their work folder.

**Then wire in toolkit policy**: one line in `~/.claude/CLAUDE.md` so the toolkit's working rules
load in every session and update with it. From the toolkit checkout:

```bash
python3 lib/team_policy.py ensure .
```

It prints one line of JSON. Act on `result`:
- `wrote`: say what it did, using its `detail`.
- `unchanged`: nothing to say.
- `opted-out`: the toolkit is not managing policy here (`team_policy.import` is false in their config); leave it.
- `refused` (exit 1): show the `detail` and change nothing. Never hand-edit `~/.claude/CLAUDE.md` around it; get help.

Delete the template's header block once the file is theirs. Show them what you wrote and ask if
anything is wrong, About me especially: a wrong description quietly degrades every later session. It
is an ordinary file they can ask Claude to edit any time.

## Phase 5 — Connectors (optional)

One sentence: connectors let Claude read apps they use, each connected once by them with `/mcp`.
None is needed for this toolkit and none is connected for them; one that needs authentication is not
finished.

## Phase 5b — Their TOOLING.md

After connectors, since it records which ones came up:

```bash
cp -n TOOLING.md.template "<workspace_root>/TOOLING.md"   # -n: never overwrite
```

**Same rule as Phase 4 (run it; never hand-write the file), and it bites harder**: hand-measured
facts nobody can regenerate. If `-n` skipped, merge the template's new rows in; their measured rows
always win. It is the map of their machine, so Claude looks things up instead of asking. Until the
map exists, an enabled tooling digest prints a notice at every session start, which stops once the
map exists or when `tooling_digest.enabled` is false. **Ask them nothing**: fill it by observation,
reading `references/tooling-map.md` and following it.

## Phase 6 — Check it works, then restart

Compare the checkout with the last record first, as before the install (`python3 lib/checkout_fingerprint.py`): identical, run this; any difference or an error, drop `CLAUDE_INSTALL_SKIP_SUITES=1` so the tests run again.

```bash
CLAUDE_INSTALL_SKIP_SUITES=1 ./install.sh --check
```

A non-zero exit lists any failed required components; report each. Then tell them to **restart
Claude Code now**, because hooks load only when a session starts, and to open the new session **in
their work folder**, the `workspace_root` Phase 1 set or confirmed: that is how Claude finds the
right memory for their work. If the install printed that `autoMemoryDirectory` is not set because no
session has run in `workspace_root` yet, that is expected now; verify finishes it.
`/restart` and resume do not count: they reopen this conversation, and verify needs a fresh session. Say too that every other open Claude Code window runs without the new guards until it, too, restarts.

```bash
cd "<workspace_root>" && claude
```

(In VS Code: open that folder, then Claude Code.) There they type `/toolkit-core:onboard verify`. Stop there.

## Phase 6b — Prove it in the new session

This runs only after the restart, from `/toolkit-core:onboard verify`. Do the steps in order, each
as your own tool call.

1. **Find the toolkit checkout from the plugin registration**, never from a symlink. If this exits non-zero or prints nothing, stop: they need to run `./install.sh` from the checkout first.

   ```bash
   claude plugin marketplace list --json | python3 -c 'import json,sys; m=[e for e in json.load(sys.stdin) if e.get("name")=="claude-code-toolkit" and e.get("source")=="directory"]; print(m[0]["path"]) if m else sys.exit(1)'
   ```

2. **Check that the guard refuses, through the harness.** First make a fresh path and prove it does not exist, in one Bash call:

   ```bash
   python3 -c 'import os, secrets, sys; p = "/toolkit-proof-" + secrets.token_hex(8); sys.exit(1) if os.path.lexists(p) else print(p)'
   ```

   Then, as its own Bash tool call, run this with `/toolkit-proof-<id>` replaced by exactly the path it printed:

   ```bash
   rm -rf /toolkit-proof-<id>
   ```

   It is harmless if it runs: the path does not exist. It should be refused, with text containing `BLOCKED by claude-danger-guard`. If it ran, they are **not protected**. If it was refused without that text, another layer refused it and the guard is not proven: say so.

3. **Check the hooks fired in this session, on the current config.** Exit 0 is required. It proves each selected profile plugin's hooks fired and prints its readiness; an "unverified" line means the Claude Code version is not the tested one, which you report but is not a failure. From a plain terminal it can prove nothing, and says so.

   ```bash
   "<checkout>/install.sh" --verify-live
   ```

4. **Check each selected plugin ships its skills and agents.** Run `python3 <checkout>/lib/profiles.py selected <checkout>` for the selected profiles. For each, take its `plugin` name from `<checkout>/claude/profiles.json` (`toolkit-core` for core), run `claude plugin details <that plugin>`, and require `Skills (N)` to equal the number of `<checkout>/skills/<profile>/*/SKILL.md`; count with `ls`. For builder, `Agents (N)` must equal the number of `<checkout>/agents/*.md`. Report each as a named pass or fail.

5. **Check Claude's memory points at their work folder.** It prints one line of JSON and changes nothing:

   ```bash
   python3 "<checkout>/lib/auto_memory.py" --status
   ```

   Act on its `action`:

   - `ok`: pass.
   - `set` or `remove`: the installer has one more change to make. Run it once more (the tests already ran in Phase 2), then the status command again; it must now read `ok`.

     ```bash
     CLAUDE_INSTALL_SKIP_SUITES=1 "<checkout>/install.sh"
     ```

   - `skip`: read `reason`. If it starts `no session has run in workspace_root yet`, this session is in the wrong folder: tell them to reopen Claude Code in the folder the JSON's `workspace_root` names, as Phase 6 said, and run `/toolkit-core:onboard verify` again. Any other reason (`no workspace_root configured`, `workspace_root is not a directory`, `project key over 200 chars ...`) is a setup problem a restart cannot fix. Report it as written and change nothing yet. Phase 1's commands use paths inside the toolkit and this session is in their work folder, so first run `cd "<checkout>"`, then go back to Phase 1's configuration step. Do not tell them to restart.
   - `foreign`: that setting is theirs. Leave it alone and tell them its value.
   - `error`: their `~/.claude/settings.json` could not be read as settings; `reason` says why. Report it, change nothing, and get help.

   Never edit `settings.json` by hand here; `install.sh` is the only writer.

6. **Check toolkit policy loads in every session.** It prints one line of JSON and changes nothing:

   ```bash
   python3 "<checkout>/lib/team_policy.py" --status "<checkout>"
   ```

   Act on `state`:
   - `ok` or `opted-out`: pass.
   - `missing` or `dangling`: run `python3 "<checkout>/lib/team_policy.py" ensure "<checkout>"` once, then the status command again; policy loads from the next session on. The new state must not be `missing` or `dangling`; act on it through this same list.
   - `legacy-copy`: their workspace `CLAUDE.md` still carries copies of policy sections that now load from the toolkit; `detail` names each. Offer to remove them, asking first, because it is their file. For each named section, compare it with the same section in `policy/CLAUDE.md`. Keep only lines they wrote, as personal additions under a heading of their own, and remove the rest of the section. Then run the status command again; it must read `ok`.
   - `error`: report `detail` as written, change nothing, and get help.

If steps 2 and 3 both pass, tell them they are protected, in those words. If either fails, tell them
directly they are **not protected**, show the one message line, and get help rather than saying
they're finished. Steps 4 to 6 are reported separately; memory and toolkit policy are not part of
"protected".

**Secret scanning is reported separately too, from this probe, never from memory:**

```bash
python3 "<checkout>/lib/secret_scan_status.py" "<checkout>"
```

Only `active` means commits are scanned for secrets. Anything else: tell them plainly that **secret
scanning is off**, and that `"<checkout>/scripts/setup-git.sh"` turns it on.

### Bring existing work over (optional)

**Offer this only when steps 2 and 3 both passed**: moving folders on an unprotected machine is the
wrong order. Many people have work elsewhere: project files in Documents, an old Claude project
folder, repositories cloned before today. This moves it into their work folder, `<workspace_root>`,
in the layout they already use there. Ask one `AskUserQuestion`: **look for existing work now**
*(recommended)*, or **skip** (they can ask later). On skip, go to *Close*.

1. **Survey, read-only.** Look in their home folder, `~/Documents`, `~/Desktop` and `~/Downloads` for work folders: anything holding a `CLAUDE.md`, any git repository, project working files. Skip `<workspace_root>` itself, `~/Library` and hidden folders. **Leave anything in a cloud-synced folder where it is:** Google Drive, iCloud Drive, Dropbox or OneDrive (`~/Library/CloudStorage`, `~/Library/Mobile Documents`, `~/Dropbox`, `~/OneDrive`). Judge each folder by its real path (`realpath`), against the markers Phase 1's workspace check uses (`CLOUD_MARKERS` in `lib/setup_config.py`), because a shortcut can point into one; moving out of a shared drive removes it for everyone. Move nothing yet.
2. **Show a numbered table**: what each folder is, and a proposed new home inside `<workspace_root>` that follows the layout already there (if it is empty, ask once how they'd like it organised). Notes they wrote for Claude merge into their `CLAUDE.md` files, asking first; finished documents shared with others stay where they are shared. Mark a row **keep in place** when moving it would break something: a git repository whose `.git` is a *file* (a linked worktree), a repository with linked worktrees of its own (`git worktree list` shows more than one), or anything a running app or script points at by path that they name.
3. **Nothing moves until they name rows**, for example "do rows 1, 2 and 4". Say that back, at their level, before acting.
4. **Move each named folder whole, with `mv`, never copying and deleting.** If the destination already exists, stop on that row and ask; never merge into or overwrite it. A folder's own `CLAUDE.md` travels with it.
5. **Show what moved where**: the same table with a done column, naming any row that did not move and why.

Then say once, plainly: **past conversations stay with the old folder.** Claude Code files chats by
folder location, so earlier chats about a moved folder stay listed under its old location; their
work and notes come along.

### Close

Close with a short summary at their level. Cover only this:

- **What's now watching out for them:** Claude refuses commands that could delete the wrong thing and keeps work in the right copy of a project. Secret scanning on commits only if `lib/secret_scan_status.py` printed `active`; otherwise say it is off and how to turn it on. Add only what they turned on: clean handovers of long conversations and automatic session titles come with continuity.
- **Commands worth knowing**, one line each: `/toolkit-core:onboard` to redo any of this; `/handover` for a conversation getting long, only if `lib/profiles.py selected` lists `continuity`; `/build` for bigger multi-file work, only if it lists `builder`. For one not selected, say which profile brings it and how to add it: the Phase 2 `--profiles` command with it added (keeping `continuity` unless they declined it), then `./install.sh` in the toolkit checkout.
- **Two habits**, no more: a new conversation for each new piece of work rather than one endless thread; and if Claude seems to misunderstand what they do, the fix is their `<workspace_root>/CLAUDE.md`, which they can ask Claude to update.
- Point them at `ONBOARDING.md` in the toolkit checkout for the fuller explanation, and say it's fine not to read it.

End by asking if they want to try something real together as a first task. Don't summarise the whole
setup back at them; they watched you do it.
