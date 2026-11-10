# claude-code-toolkit

Safety rails and workflow tools for Claude Code on a Mac: guards that check
commands before they run, session continuity, a planned-then-built workflow for
bigger changes, and the small command-line tools behind them. Free for your own
individual use (see [License](#license)).

What each profile installs, and what each of its hooks writes on its own:
[docs/catalogue.md](docs/catalogue.md) (generated from the repo).

What you get, one line each:

- **Known destructive commands are blocked.** A guard refuses a set of genuinely
  destructive command shapes, and judges every `rm` by where it points rather
  than how it's spelled. It is a backstop, not a sandbox.
- **Work lands in the right checkout.** Git commands that reach into a different
  checkout are refused, and two sessions editing one repo are kept apart.
- **Long threads hand off cleanly.** You're nudged to write a handover before a
  session gets too big, and the next session is told it exists.
- **Every session gets a title**, so `csearch` finds last Tuesday's work.
- **Claude sees its own usage budget** where Claude Code exposes it, and says
  "unknown" rather than going quiet where it doesn't.
- **Credentials stay out of git.** An optional pre-commit scan blocks a commit
  that contains an API key.
- **Big builds split across models** (optional): Opus plans and reviews, Sonnet
  implements, and review gates run before anything merges.

Nothing here is specific to one person or machine. Your paths and preferences
live in one config file **outside** the checkout, so the same clone works for
anyone.

## New here?

**Read [ONBOARDING.md](ONBOARDING.md), or ask Claude to read it and follow it.**
It walks a Mac from nothing to a verified setup, and Claude does almost all of
it: it detects what you already have, asks only what it needs, explains each
tool before turning it on, and checks the result. Once setup has run, the same
walkthrough is the `/onboard` command (it doesn't exist before then). This
README is the reference; ONBOARDING.md is the path.

## Install

You need macOS, Claude Code (the `claude` command-line tool; tested with 2.1.306), `git` and
`python3`. GitHub's `gh` CLI, Codex and a password manager are all optional.
If `claude --version` says `command not found` in a new Terminal window after
installing Claude Code, put `~/.local/bin` on your PATH: ONBOARDING.md Step 1
has the lines to paste.

```bash
git clone https://github.com/mouramarket/claude-code-toolkit.git ~/claude-code-toolkit
cd ~/claude-code-toolkit
./install.sh                  # asks a few questions, writes your config, links the tools, runs the tests
./scripts/setup-git.sh        # optional: git defaults + the pre-commit credential scan (sets core.hooksPath globally)
./scripts/setup-plugins.sh    # optional: recommended plugins from Anthropic's official marketplace
```

Or skip all of that: open Claude Code in the checkout and send **"Set me up:
read ONBOARDING.md and follow it."** Claude runs the same sequence
conversationally and recommends rather than interrogates. Afterwards `/onboard`
reruns or repairs it.

On a first run `install.sh` asks for your first name, your workspace root (the
folder your work lives in), where handovers go, an optional folder for planning
specs, and whether to use Codex for review. It writes
`~/.config/claude-code-toolkit/config.json`, links the toolkit's commands into
`~/.local/bin`, enables the plugin for each profile you chose, applies the
baseline permissions and runs the test suites.

Then **restart Claude Code**, opening the new session in your work folder
(`<your-workspace-root>`). Hooks are read only when a session starts, so nothing
applies to a window that was already open. In the new session run
**`/toolkit-core:onboard verify`**: it proves the guard refuses a dangerous
command in that session and that its hooks fired. Setup isn't finished until it
passes.

```
./install.sh --check        # show what would change, change nothing, never prompts
./install.sh --links-only   # which links are missing (about a second; primary checkout only)
./install.sh --verify-live  # run by Claude inside a session: did this session's hooks fire, for every selected profile?
./install.sh --setup        # redo the questions and rewrite your config
./install.sh                # re-run any time; safe to repeat
./install.sh --update       # fast-forward to the latest toolkit, then re-run the installer
./install.sh --version      # which commit you are running, and whether you're behind
./install.sh --rollback     # go back to the commit before your last --update, and stay there
./install.sh --autonomous   # ADVANCED opt-in, see Permissions
./install.sh --uninstall    # see Uninstall
```

The installer is conservative. It never overwrites a real file it didn't
create, takes a dated snapshot of `~/.claude/settings.json` before changing it,
and leaves your `permissions.allow`, `model`, `theme` and other plugins alone.
It records what it added, so update, rollback and uninstall remove only the
toolkit's own entries.

**Already have a custom `statusLine`?** The installer leaves it alone and says
so. The usage tools need the status line's input, so make your command also pass
its stdin to `~/.local/bin/claude-usage-line` (or call that and append your own
segments to what it prints). Until then the usage hooks report the budget as
UNKNOWN once per session instead of going quiet.

**Run the installer only from the primary checkout, never from a git worktree.**
From a worktree it would repoint every link, the guards included, at a directory
that disappears when the branch merges. It refuses by default.

## Configure

Setup writes the config for you and `./install.sh --setup` rewrites it, so you
should never need to open it. `config.example.json` documents every key and the
default it falls back to; every key is optional.

The one that matters is `workspace_root`, the tree your work lives in. The
danger guard treats deletions inside it as ordinary and deletions outside it as
suspect. **It has no default on purpose:** with none set, the guard trusts only
`~/.claude`, `~/bin` and the temp directories, so an unconfigured machine errs
strict.
Setup refuses your home directory, the filesystem root and anything inside a
cloud-synced folder (iCloud Drive, Google Drive, Dropbox, OneDrive): a delete
there reaches every machine you own.

## Profiles and skills

A profile is one plugin in this repo's `claude-code-toolkit` marketplace.

| Profile | Plugin | What it adds | Default |
|---|---|---|---|
| core | `toolkit-core` | The guards, integrity checks and `/onboard` | always |
| continuity | `toolkit-continuity` | `/handover`, `/wrap-up`, the memory and CLAUDE.md distillers, usage budget, session titles | yes |
| builder | `toolkit-build` | `/build` and its executor and reviewer agents | no |
| maintainer | `toolkit-maintainer` | `/review-pr-ci` and `/skill-builder`; brings builder and `skill-prompt-optimizer` | no |
| mac | `toolkit-mac` | `/caffeinate` | no |

To change profiles, from the primary checkout:

```bash
python3 lib/setup_config.py ~/.config/claude-code-toolkit/config.json "$(pwd)" --update --profiles continuity,builder
./install.sh
```

A list replaces the default, so keep `continuity` in it (core is always added).

**Skills on their own.** Three general-purpose skills ship as separate plugins
with no hooks: `skill-prompt-optimizer`, `skill-humanizer` and
`skill-claude-teacher`. You can use them without installing the rest:

```bash
claude plugin marketplace add mouramarket/claude-code-toolkit
claude plugin install skill-humanizer@claude-code-toolkit
```

Claude Code doesn't auto-update a marketplace like this one unless you turn it
on (`/plugin` → Marketplaces → Enable auto-update). To pick up changes by hand:

```bash
claude plugin marketplace update claude-code-toolkit
claude plugin update skill-humanizer@claude-code-toolkit
```

## Permissions

Two sets, and the difference is deliberate.

**Baseline, applied by a plain install.** It only ever *narrows* what Claude may
do: `deny` rules on credential folders (`~/.ssh`, `~/.aws`, `~/.gnupg`, `gh`'s
config, `~/.netrc`) and `ask` rules on pushing, resetting, cleaning, dropping a
stash, opening or merging a PR, cutting a release and publishing a package.
Entries are added to your existing settings; nothing is removed.

**Autonomous, opt-in only, via `./install.sh --autonomous`.** ⚠️ Adds `Bash(*)`
and broad tool grants under `defaultMode: "auto"`, so almost nothing prompts.
It needs a TTY and the typed phrase `I understand`, and a plain install never
applies it. Read `claude/permissions.autonomous.json` in full first: it spells
out what the guard does not catch. The `/autonomy-permissions` skill tunes
toward the same mode and is slash-only, so Claude never runs it unasked. Both
make sense only alongside a written rule in your CLAUDE.md saying when Claude
must stop and ask.

## Your CLAUDE.md and the toolkit policy

`CLAUDE.md.template` holds the sections about you, marked `TODO`. `/onboard`
copies it into your workspace root and fills it in with you; by hand, copy it
and fill it in yourself.

How the toolkit expects Claude to work (guard and merge stops, evidence rules,
where notes go) lives in `policy/CLAUDE.md`. `/onboard` adds a two-line import
of it to `~/.claude/CLAUDE.md` (`python3 lib/team_policy.py ensure <checkout>`
does the same). **Updating the toolkit replaces that file**, so never record
anything personal in it: your workspace `CLAUDE.md` loads after it and overrides
its defaults. To opt out, delete the two lines and set `team_policy.import` to
`false`.

## What's in here

| Path | What it is |
|---|---|
| `tools/danger-guard/` | Checks every shell command before it runs. Blocks destructive shapes (`sudo`, `curl \| sh`, force pushes, raw disk writes, credential reads) and judges `rm` by its target. Fails open on anything it doesn't recognise; fails strict on a missing config. |
| `tools/worktree-guard/` | Rule A: refuses a git command reaching into a different checkout than the one you're in. Rule B: once a repo has two or more worktrees, refuses branch switches, hard resets and stashes in the shared checkout. Rule C (off unless `worktree_guard.canonical_root` is set): refuses work landing directly on a canonical checkout's default branch. Escape hatch `WORKTREE_ALLOW=1` (`WT_ALLOW_MAIN=1` is a synonym). |
| `tools/session-claim/` | (builder) Moves a second session that starts writing into a checkout another live session holds into its own worktree. |
| `tools/base-pin/` | (builder) Says once, mid-session, when `origin/<default>` has moved ahead of the worktree you're in. |
| `tools/secret-scan/` | `claude-secret-scan` and the pre-commit hook that runs it. Installed by `scripts/setup-git.sh`, not `install.sh`; until then `install.sh` reports secret scanning as NOT active. Names the file and line, never the value. |
| `tools/toolkit-integrity/` | Says loudly when a configured hook can't run (Claude Code skips it silently), canaries the guards at session start, and audits recent sessions for shell commands that ran with no guard heartbeat. |
| `tools/repo-freshness/` | Says once per session when the toolkit checkout, or a repo you list under `repo_freshness.watch`, is behind its remote, with the command to fix it. Auto-pulling is off unless `repo_freshness.sync.enabled` is `true`. |
| `tools/tooling-digest/` | Injects a short summary of your `TOOLING.md` (what's installed and connected) at session start. With the digest on and no map, it prints a notice every session until you create one from `TOOLING.md.template` or set `tooling_digest.enabled` to `false`. |
| `tools/session-titler/` | (continuity) Titles a session when it ends: a plain outcome phrase, best effort, for sessions with at least two real messages, using one detached `claude -p --model haiku` call each. `csearch` searches the titles. |
| `tools/thread-handover/` | (continuity) Nudges for a handover at a natural boundary. Handovers are written to `thread_handover.handover_dir`, announced (never auto-loaded) at session start, claimed when a prompt names the file (paste the path; `@` doesn't find it in VS Code), and archived, never deleted, after `sweep_grace_days`. |
| `tools/usage-watch/` | (continuity) A status line that caches the 5-hour and 7-day limits, and hooks that warn the main loop at 70/85/95%, tell each subagent its headroom, and write a handover if a turn dies on a limit. The status line runs in the terminal only; elsewhere the hooks say UNKNOWN. A transcript-based estimator ships off (`usage_watch.estimate`): one anchored figure (`claude-usage-anchor <pct> --resets-in <dur>`, or automatic from a terminal status line), with the ratio measured from two readings 5+ points apart in one window or taken from `usage_watch.default_ratio`. Read that key's notes in `config.example.json` before enabling it. |
| `tools/memory-routing/`, `tools/context-growth/`, `tools/memory-lint/`, `tools/context-budget/` | (continuity) Nudges when a note lands in the wrong file or a memory or CLAUDE.md grows past its cap; `/memory-distill` and `/claude-md-distill` fix them. |
| `tools/wrap-up/`, `tools/backup/` | (continuity) `/wrap-up` closes a day or a window. `claude-backup` is opt-in: set `backup.destination` to an existing folder your cloud storage syncs, and it copies your workspace root (minus `wrap_up.repos_root`), every auto-memory folder, the toolkit config and `settings.json`, skipping binaries. Until then `/wrap-up` reports backup as not configured. |
| `tools/setup-audit/` | `claude-setup-audit`, run by hand and by `/onboard`, never a hook. `audit`/`--json` is read-only; `snapshot` only appends; `apply-safe` removes only files byte-identical to the toolkit's own copy, after another snapshot. Reports facts, never whether a difference matters. |
| `tools/build-route/`, `tools/build-lens/`, `tools/codex-review/`, `tools/merge-brief/`, `tools/model-guard/`, `tools/build-cost/`, `tools/spec-archive/`, `tools/prompt-optimizer/` | (builder) The `/build` workflow: review mode, gate references, the optional Codex reviewer, blinded briefs, the model guard on dispatches, a per-session cost ledger, archiving shipped specs (inert until `spec_archive.specs_dir` is set) and the optional prompt reminder. |
| `tools/local-ci/` | `claude-local-ci` runs `ci/local-checks.sh` against a head merged with its base and records a receipt. |
| `tools/caffeinate/` | (mac) Holds a macOS sleep assertion while Claude is working. Off unless `caffeinate.enabled`; plugged in only, until `/caffeinate on`, which releases below `battery_floor_percent` (20). |
| `tools/harness-check/`, `tools/skill-trigger/`, `tools/catalogue/`, `tools/leak-gate/`, `tools/hook-heartbeat/`, `tools/distill-guard/`, `tools/git-tidy/`, `tools/context-lint/` | Maintenance and support: Claude Code version checks, skill trigger tests, the catalogue generator, a term and credential gate, guard heartbeats, the distill write guard, `git tidy`, and context linting. |
| `skills/`, `agents/`, `plugins/` | The slash commands (`skills/<profile>/<command>/`), the builder agents, and the three standalone skill plugins. |
| `lib/` | Shared code: the config reader every tool uses, setup, install records, update and uninstall. |
| `claude/` | Permission sets, the profile registry and the recommended-plugin list. |
| `policy/CLAUDE.md`, `CLAUDE.md.template`, `TOOLING.md.template` | The toolkit policy, your personal CLAUDE.md skeleton, and the tooling map skeleton. |
| `scripts/` | `setup-git.sh`, `setup-plugins.sh` and the repo's layout and bundle checks. |
| `actions/claude-pr-review/` | An optional PR review action and a workflow template you copy into your own repo. |

## Optional extras

- **Full-power review.** `/build`'s gates use one Claude reviewer by default.
  Choose full-power at setup (or set `build.review`) to have Codex review
  instead; that needs Codex installed and signed in. If it isn't, the gate exits
  4, reviews nothing, and says to install and sign in or run the Claude reviewer.
  `codex_review.models` overrides the model per gate. No calibration ships, so
  the Codex budget notice works from your used percentage only: at 90% and at
  100% (`usage_watch.codex_low_gates: 0` keeps only the 100% notice).
- **`toolkit-build:architect-reviewer`.** An optional whole-branch reviewer you
  dispatch by hand. It is never a gate.
- **Prompt reminder.** `prompt_optimizer.enabled` (off by default) reminds
  Claude to sharpen a vague brief before handing it to a subagent. It needs the
  `skill-prompt-optimizer` plugin; `install.sh` says when it is on but inert.
- **The Workflow setting.** With builder selected, `build.enable_workflows: true`
  has `install.sh` set Claude Code's `enableWorkflows`, only where you have no
  value of your own. Set it back to `false` and re-run `./install.sh` to remove
  it. `/onboard` explains it before offering.
- **PR review in GitHub Actions.** Copy
  `actions/claude-pr-review/workflow-template.yml` into your repo; its header
  carries the setup (one `CLAUDE_CODE_OAUTH_TOKEN` secret). Green proves a
  well-formed review was posted for the head commit by that run, not that the
  review was good.
- **Skill evals.** `/skill-builder` works on your own skills (personal, project
  or plugin). `evals/run_eval.py --live` is opt-in and billed to your own
  `ANTHROPIC_API_KEY`.

## Keeping it current

Hooks run straight out of the checkout, so not updating changes nothing visible:
you keep running the old guards. `repo-freshness` tells you when the checkout is
behind and offers the command.

```bash
cd ~/claude-code-toolkit && ./install.sh --update
```

Then start a new session. `--update` refuses an origin that isn't this
repository unless you set `update.origin`. Running a fork? From the checkout:

```bash
python3 lib/setup_config.py ~/.config/claude-code-toolkit/config.json "$(pwd)" --update --set update.origin=https://github.com/you/claude-code-toolkit
```

If an update breaks something,
`./install.sh --rollback` returns you to the previous commit and holds you there;
`./install.sh --update` brings you forward again. A rolled-back checkout is
named every session until you do.

## Uninstall

From the primary checkout. The first command changes nothing and prints *Will
remove*, *Left in place*, *Blocked* and *Always kept*. Fix each blocked item,
run the second, then restart Claude Code.

```bash
./install.sh --uninstall
./install.sh --uninstall --yes
```

What stays, and how to remove it by hand:

- **The checkout:** delete it, only after uninstall succeeded.
- **Toolkit config:** `rm -rf ~/.config/claude-code-toolkit`
- **Settings snapshots:** `rm ~/.claude/settings.json.claude-code-toolkit-*.bak ~/.claude/settings.json.pre-claude-code-toolkit.bak`
- **Git settings from `setup-git.sh`:** it sets `init.defaultBranch`,
  `pull.rebase` and `core.excludesfile` only where you had no value. Unset only
  the ones it set (`git config --global --unset <key>`). Remove the ignore lines
  it appended (`.DS_Store`, `__pycache__/`, `*.pyc`, `.venv/`) only if you don't
  want them; never delete an ignore file you already had.
- **Recommended plugins** from `setup-plugins.sh` stay until you
  `claude plugin uninstall` them.

## Tests

The full local check is `./ci/local-checks.sh` from the repo root. To run only
the suites:

```bash
failed=0; ran=0
while read -r t; do
  ran=$((ran + 1))
  (cd "$(dirname "$(dirname "$t")")" && python3 "tests/$(basename "$t")" </dev/null) || { echo "FAIL $t"; failed=1; }
done < <(find tools lib skills evals scripts ci -path '*/tests/test_*.py' -type f | sort)
echo "$ran suites run"; [ "$ran" -gt 0 ] && [ "$failed" -eq 0 ]
```

`</dev/null` keeps a suite that reads stdin from swallowing the rest of the
queue. The suites pass with your config and with none at all
(`CLAUDE_TOOLKIT_CONFIG=/nonexistent.json`), which is what a fresh clone looks
like. No count is quoted here on purpose; the loop prints the real one.

How to work in this repo, and the decisions not to undo:
[CLAUDE.md](CLAUDE.md) and [docs/decisions.md](docs/decisions.md).

## Caveats

- **No secrets in the repo, by construction.** Credentials resolve at runtime
  from an environment variable or your password manager. A literal token here is
  a bug, and `claude-secret-scan` checks for one.
- **`setup-git.sh` sets `core.hooksPath` globally**, which replaces every repo's
  own `.git/hooks`. Each client-side hook is forwarded to the repo's own, with
  the credential scan first. If you already set `core.hooksPath`, it leaves yours
  alone and tells you. One commit can skip the scan with `git commit --no-verify`.
- **`~/.local/bin` may not be on your PATH.** Hooks are unaffected (they use
  absolute paths), but `git tidy` and `csearch` won't run by name until it is.
  `install.sh` warns.
- **macOS and Claude Code only.** Windows, Linux and other agents are not
  supported.

## License

Free for your own individual use, including paid work, and for private changes
to your own copy. Redistribution and installing it for other people are not
permitted. The full terms are in [LICENSE](LICENSE).
