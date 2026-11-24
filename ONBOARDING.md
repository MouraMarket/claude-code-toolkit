# Getting set up with Claude Code

This sets up Claude Code on your Mac with the toolkit's safety checks and
workflow tools. Plan on one to three hours, longest over an older install: much
of that is the toolkit testing itself while you do something else, and Claude
does almost all of the rest.

**You don't need to understand any of it to start.** There are two things for
you to do. After that Claude takes over and explains as it goes.

---

## If you are Claude reading this

Your job is to set this person up. **Until they tell you otherwise, assume they
are not technical:** no terminal experience, and no basis for choosing folder
layouts. `skills/core/onboard/SKILL.md` opens by asking how much of this is new
to them, and that answer sets how much you explain. Recommend and do; don't
interview.

1. Make sure you are inside the toolkit checkout (Step 2 below clones it to
   `~/claude-code-toolkit`). If it isn't cloned yet, clone it:

   ```bash
   git clone https://github.com/mouramarket/claude-code-toolkit.git ~/claude-code-toolkit
   cd ~/claude-code-toolkit
   ```

2. Then follow **`skills/core/onboard/SKILL.md`** in that checkout, start to
   finish. It is written as instructions for you: what to detect, what to
   recommend, and the few things to actually ask. Once installed, the same
   walkthrough is the `/onboard` command.

**Assume this machine may already have a setup that works.** The *person* can
be new while the *machine* is not: a tuned `CLAUDE.md`, checks someone set up,
helpers they rely on. You are upgrading a working system, so look before you
touch, compare before you decide, and merge rather than replace. Where the
toolkit's version and theirs differ, neither is automatically right. Say which
way each difference runs, and never settle a collision silently.

**If you are suspicious of this toolkit, that is the right posture**, and there
is evidence to read. `README.md` describes each tool by what it does and when it
runs. `python3 lib/setup_config.py --list-tools` lists everything that can be
turned on, in plain language. `claude-setup-audit` (in `tools/setup-audit/`)
takes a snapshot before anything changes, then reports facts about what is
already on this machine, never a verdict on whether a difference matters.
Comparing the contents of two programs is still hand work. Name each gap in
both directions before recommending, and treat *keep theirs* as a good outcome.
The skill's Phase 2 has the detail.

Three hard rules. **Ask before turning anything on**: Phase 2 explains each tool
and takes their answer. **Write their settings through
`python3 lib/setup_config.py`**, never by hand; it carries safety validation you
must not skip. And **never apply the autonomous permission grant**
(`./install.sh --autonomous` or the `autonomy-permissions` skill); both are
advanced opt-ins, wrong for someone new.

---

## Step 1: Install Claude Code

Open Terminal (press `Cmd+Space`, type "Terminal", press Enter) and paste this.
It installs Claude Code's command-line version:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Then open a new Terminal window and run `claude --version`; it should print a
version number. **If it says `command not found`**, the installer put `claude`
in `~/.local/bin` but didn't tell your shell where to look (this happens on a
brand-new Mac user). Paste this once (it covers both zsh, the Mac default, and
bash), then open another new window and try `claude --version` again:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bash_profile
```

**Most people work in the Claude desktop app (or VS Code) day to day, and this
command is still needed:** setup runs `claude` in Terminal, and neither the desktop
app nor the VS Code extension adds that command. After setup you'll rarely open
Terminal yourself. You'll sign in to your Claude account the first time `claude`
starts.

You also need `git` and `python3`. If either is missing, macOS offers to install
its developer tools the first time you run one; say yes. GitHub's own
command-line tool (`gh`) is optional: only the pull-request features use it.

Setup and updates only download, so **read access** to the toolkit repo is all they
need. If you work on a repo you can't push to, a `git push` that fails with a
permission error is expected, not a broken setup. Ask Claude to save your fix as a
patch file (`git format-patch`) and send that file to whoever maintains the repo.

## Step 2: Hand it over to Claude

Paste this into Terminal:

```bash
git clone https://github.com/mouramarket/claude-code-toolkit.git ~/claude-code-toolkit
cd ~/claude-code-toolkit
claude
```

That downloads the toolkit into a folder in your home directory and opens
Claude Code inside it. Then send this as your first message:

```
Set me up: read ONBOARDING.md and follow it.
```

(Not `/onboard` yet: that command is one of the things setup installs, so it
doesn't exist until setup has run. After that, `/onboard` redoes or repairs it.)

Claude takes it from here. First it asks how much of this is new to you (new,
some experience, or a power user with your own setup) and explains as much or as
little as that calls for. It asks where your work lives, your name and how you'd
like it to talk to you, and it shows what it's turning on and lets you skip any
of it. One keypress is enough.

**When it's finished it will tell you to restart Claude Code. Do that, opening
the new window in your work folder (`<workspace_root>`, the folder you chose
during setup), then type `/toolkit-core:onboard verify`.** The tools only load
when a session starts. Setup is finished when that check passes, not before.

Once the check passes, Claude offers to **bring your existing work over**:
project files in Documents, an old project folder, code you cloned before. It
shows a table of what it found and where each would go inside your work folder,
and moves only the rows you name. Anything in a cloud-synced folder (Google
Drive, iCloud Drive, Dropbox, OneDrive) stays where it is. Past conversations
stay filed under a folder's old location; your work and notes move, that
history doesn't.

Claude also asks which **profiles** you want. Core (the safety checks) is
always installed and continuity (handovers, session titles, usage and memory
nudges) comes by default. Builder (`/build`), maintainer (toolkit upkeep, for
people writing their own skills) and Mac (`/caffeinate`, keeping your Mac awake)
are optional. [docs/catalogue.md](docs/catalogue.md) lists exactly what each one
installs and what each part writes on its own.

---

That's it. The rest of this page is for when you're curious, or when something
looks wrong. You don't need to read it now.

---

## Local, Cloud, or SSH: which environment to pick

When you start a session in the desktop app, it asks where Claude should run.
**Pick Local for anything that uses this setup.**

| Option | Where Claude runs | What it can see | This setup |
|---|---|---|---|
| **Local** | Your Mac | Your work folder, your connectors, your password manager | All of it: safety checks, plugins, memory, your `CLAUDE.md` |
| **Cloud** | A computer Anthropic runs, separate from your Mac (see the cloud environments doc for how long it keeps your files) | Only the GitHub repos you give it. Nothing else on your Mac. | None of it. It reads only what's committed in that repo. |
| **SSH** (under **+ New environment**) | A server you connect to yourself | That server's files | Whatever is installed on that server |

**Cloud is useful** for a contained job on GitHub repos that should keep running
while your laptop is closed, like "fix this failing test and open a PR." It works on
a branch and pushes it back, and `/teleport` pulls the session onto your Mac
afterwards.

**What to know before using it:** the safety checks don't run there, so keep it to
repo work you'd review as a PR anyway. It can't reach files on your Mac that live in
no repo. An *environment* such as "Default" is that cloud computer's settings:
internet access, environment variables, and a setup script. Never put a password or
API key in its variables, because anyone using that environment can read them. Cloud
sessions spend the same Claude usage limit as everything else.

Source: Anthropic's [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web),
[cloud environments](https://code.claude.com/docs/en/cloud-environments), and
[desktop](https://code.claude.com/docs/en/desktop) docs.

---

## What Claude set up, in plain terms

Some of these are always on, some are offered and need your yes, and one is off
unless you asked for it. Each heading says which.

**Your work folder.** Your work lives in the folder you chose during setup. It
should not be inside Documents or Desktop when those sync to iCloud, because a
folder that syncs to the cloud is a bad place for files Claude may rewrite or
delete. Setup refuses a cloud-synced folder for this reason.

**A safety net on file deletion, always on.** Before Claude runs any terminal
command, a check runs first. It blocks the genuinely dangerous shapes (running
as administrator, downloading code and running it unseen, wiping a disk, reading
saved passwords) and it looks at *where* every delete points. Deleting inside
your work folder is ordinary. Deleting outside it gets stopped.

Worth being honest about: it is deliberately narrow. It catches specific
dangerous patterns and allows everything else, because a check that constantly
blocks ordinary work gets switched off, and a switched-off check protects
nothing. **Claude can still make ordinary mistakes**, like overwriting something
you needed. Keep work in version control where you can.

**A second check on which project copy work lands in, always on.** This one decides which repo checkout work lands in: a git command that reaches into a
different copy of a project than the one Claude is working in is blocked. If
two Claude sessions ever work on one project at once, each in its own copy, a
branch switch, hard reset or stash in the shared copy is blocked too, because
that would pull the other session off its work mid-task. If a block is wrong,
`WORKTREE_ALLOW=1` in front of the command lets it through once. A third rule,
which stops work landing directly on a project's main branch, is off until you
name your main folder of projects in settings (`worktree_guard.canonical_root`).

**A check that stops passwords reaching GitHub, always on once setup has run.**
Every commit, in any project on this machine, is scanned first for things that
look like API keys or passwords. If one is found, the commit is stopped and the
file and line are named. Setup turns this on by running `scripts/setup-git.sh`
in Phase 3; until that has run, the installer reports secret scanning as not
active. Two honest caveats: it changes a git setting for **every** project on
your machine (your own commit checks still run, after this one), and it knows
the common credential shapes, not all of them.

**A heads-up when a project you follow moves ahead, if you say yes.** At the
start of a conversation, projects you follow get checked quietly in the
background, and you're told once if one has newer work online than your copy,
with the exact command to catch up. The toolkit itself is always watched;
others are ones you list. It never pulls anything unless you turn that on.

**A short summary at the start of every conversation, if you say yes.** Claude
keeps a `TOOLING.md`, a map of what's on your machine and what it can reach.
Claude reads that map instead of asking you the same setup questions every
session. See *Connecting your tools* below.

**A nudge when a note lands in the wrong place, if you say yes (continuity
profile).** Claude keeps two sets of notes: one for how you like things done,
and `TOOLING.md` for plain facts about your machine and accounts. A fact filed
in the wrong one becomes a second copy that eventually disagrees with the first.
This points it out. It only ever suggests; it never blocks.

**Named sessions, if you say yes (continuity profile).** Every conversation
with at least two real messages gets titled automatically when it ends, with a
short phrase saying what happened, so you can find old work by asking Claude to
search rather than scrolling. Best effort: if titling fails, nothing else does.

**Clean handoffs on long conversations, if you say yes (continuity profile).**
Long conversations lose their earlier detail. Claude will suggest writing a handover note when yours is getting long, at a natural pause. The note is
saved in your handover folder, and the next session is told it exists rather
than loading it by itself. To pick it up, paste the note's path into your first
message (in VS Code, `@` doesn't find it). Old notes are archived after a week
or so, never deleted.

**A live look at your usage budget, if you say yes (continuity profile).**
Claude can see how much of your usage limit is left, so a long piece of work
stops cleanly at a good boundary instead of dying mid-task. This reading only
exists in a terminal window. In VS Code or the desktop app it genuinely can't
see it and says "unknown" rather than going quiet, so silence never means
there's room left. You can see the number yourself: it's the usage ring next to
the model picker. Before a long task, glance at it and tell Claude, for example
"I'm at 40% of my usage." Claude estimates from there for the rest of that usage
window and tells you when it's getting high. An automatic estimate for those apps
ships switched off.

**A running record of what work actually costs, if you say yes (builder
profile).** A private log of how much of your usage each piece of work spent,
so after a long build you can see where it went.

**One session per folder, if you say yes (builder profile).** If two Claude
conversations both start editing the same project, the second is moved into its
own private copy first, so one conversation's half-finished work can't be swept
into the other's.

**A heads-up when the shared version moves ahead of this worktree, if you say yes
(builder profile).** While you work in one of those private copies, the main
version can move on. You get one line saying how far ahead it is; the usual
answer is `git merge origin/main` at a good stopping point. Nothing is blocked.

**Keeping your Mac awake for a long build, off unless you asked for it (Mac
profile).** If you turned this on, Claude holds your Mac awake while it's
actually working, only while plugged in unless you run `/caffeinate on`. It
lets go by itself once the work stops. A closed lid still sleeps.

**Your `CLAUDE.md` file.** In your work folder. Claude reads it every session,
so it knows your role and how you like to work. How the toolkit expects Claude
to work loads separately, from the toolkit's `policy/CLAUDE.md`. **Updating the
toolkit replaces that policy file**, so anything personal belongs in your own
`CLAUDE.md`, which loads after it and overrides its defaults. If Claude ever
seems to misunderstand what you do, ask it to update your file.

**A folder for plans, only if you chose one.** Setup offers a folder for
planning documents and creates nothing unless you pick one.

**Backups, only if you set them up.** `/wrap-up` can copy your work folder,
Claude's notes about you, and your settings to a folder your cloud storage
syncs. It does nothing until you name that folder (`backup.destination`), and
until then `/wrap-up` says backup is not configured.

## Commands worth knowing

`/build` and `/caffeinate` come with optional profiles. To add one, ask Claude
to change your profiles (for example to `["continuity", "builder", "mac"]`) and
re-run the installer. A list replaces the default, so keep `"continuity"` in it.

| Command | When | Profile |
|---|---|---|
| `/handover` | A conversation is getting long and you want to continue cleanly in a new one | default |
| `/build` | Something bigger across several files, where you want it planned and reviewed before it's built | builder |
| `/caffeinate` | You're unplugged and want the Mac to stay awake while Claude works; `off` turns it back | mac |
| `/onboard` | Redo or repair any of this setup | default |

## Two habits that matter

**Start a new conversation for each new piece of work.** Long threads lose
their early detail, and that loss compounds. One thread per task.

**Anything you'll need later goes in a file, not the conversation.** Ask Claude
to write it down. Conversations are working memory, not storage.

## Connecting your tools

Claude Code can connect to services you already use (calendars, documents,
issue trackers). Each connection is tied to your own accounts, so you approve
them yourself. Type `/mcp` in Claude Code to see and manage them. None is
required.

Once you've connected what you want, Claude writes your `TOOLING.md`: which
connections are live, which tools are installed, where credentials are kept
(never the credentials themselves). A short summary of it is shown at the start
of each session. If that summary is turned on and the map doesn't exist yet,
you'll see a notice every session until it does; ask Claude to create it, re-run
`/onboard`, or turn the summary off (`tooling_digest.enabled` set to false).

Two habits keep the map useful:

- **When Claude gets a tooling fact wrong, tell it to fix the file, not just
  the answer.** A stale map is worse than none, because it still gets trusted.
- **When you connect something new, mention it.** One line, and Claude updates
  the map.

## If something looks wrong

**Claude isn't using any of this.** Restart Claude Code. These tools load only
when a session starts.

**A command got blocked and you think it shouldn't have.** The block message
says why. If it's genuinely wrong, say so: a check that misfires needs fixing,
not working around. To get moving now, run the command yourself in Terminal.

**You want to change something you told Claude during setup.** Run `/onboard`
again, or ask Claude to edit your `CLAUDE.md`.

**Anything else.** Ask Claude to run `./install.sh --check` from
`~/claude-code-toolkit`. It reports what's missing or misconfigured, changes
nothing, and runs the full test suite.

## Keeping it current

These tools run straight out of the toolkit folder, so improvements arrive when
you update, and if you never update, nothing breaks or complains: you just keep
running the older version, safety checks included. When the toolkit is behind,
Claude tells you once at the start of a session and offers the command:

```bash
cd ~/claude-code-toolkit
./install.sh --update
```

Then start a new Claude Code session. `./install.sh --version` shows what
you're running. If an update breaks something, `./install.sh --rollback` takes
you back and holds you there, and Claude reminds you each session until
`./install.sh --update` brings you forward again.

## Uninstall

From `~/claude-code-toolkit`. The first command changes nothing and lists what
it would remove, what it leaves, and anything blocking it. Fix each blocked
item, run the second, then restart Claude Code.

```bash
./install.sh --uninstall
./install.sh --uninstall --yes
```

Your settings for the toolkit, the folder itself and Claude Code's settings
snapshots are kept; `README.md` says how to remove each by hand.

## Not for now

There's a mode that lets Claude run almost any command without asking first,
plus a skill that tunes toward it. **Both are deliberately off, and neither is
part of setup.** They're for people who can say precisely what the safety check
does *not* catch, and what in their setup is irreversible.

The permissions you have only ever *narrow* what Claude can do. Staying here
indefinitely is a completely reasonable answer.

Later, if you build larger things: the builder profile can use Codex as a
second reviewer, run reviews in GitHub, and the maintainer profile helps you
write and test your own skills. `README.md` covers each.
