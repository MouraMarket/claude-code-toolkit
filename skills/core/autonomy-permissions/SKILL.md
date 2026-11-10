---
name: autonomy-permissions
disable-model-invocation: true
description: >
  Audit recent Claude Code transcripts and retune permissions so Claude runs to a decision gate
  instead of stopping at permission prompts. Use when the user is tired of approving things, wants
  Claude to "just work" / "stop asking" / "run without interrupting", wants to loosen or widen
  permissions, or mentions permission prompts, approval fatigue, acceptEdits, bypassPermissions or
  settings.json permissions. Also when they ask to review what Claude has been running, to tighten
  the deny list or guardrails, or to check whether the current permissions still match how they
  work, or after the same approvals kept recurring. Not for diagnosing a specific denied command or
  a broken hook (ordinary debugging), or for tuning one repo's project-scoped .claude/settings.json;
  it does audit <workspace_root>/.claude/settings.local.json, whose approvals silently preempt the
  auto-mode classifier.
---

> ## ⚠️ ADVANCED USERS ONLY — do not run this during onboarding
>
> This skill **widens what Claude may do without asking you first.** Taken to its
> conclusion it produces a blanket `Bash(*)` grant under `defaultMode: "auto"`,
> where almost no command prompts. Auto mode drops the blanket grant, so unmatched
> Bash goes to its classifier after the `PreToolUse` guard hook; a narrow allow
> rule skips the classifier. Outside auto mode the hook is the *only* shape check
> on shell access.
>
> That configuration is defensible, but only if you can answer these yourself:
>
> - What `claude-danger-guard` blocks, and — more importantly — what it does not.
>   It recognises a specific set of destructive *shapes*. Everything else it allows.
> - Why a wide grant demands a *stronger* obligation to stop at decision gates,
>   and what your gates are.
> - What is irreversible in your environment, and what has no backup.
>
> If any of that is unclear, **stop and leave your permissions as they are.** The
> installed baseline only ever *narrows* what Claude can do; that is the safe
> default and it is fine to stay there indefinitely.
>
> This skill ships with the core profile but is **slash-only**
> (`disable-model-invocation: true`): Claude never starts it on its own, so it
> runs only when you type `/autonomy-permissions`. Having it installed changes
> no permission, and neither does running it: every change it proposes waits
> for your approval. One person's permission configuration is not a template
> for anyone else.

# Autonomy Permissions

Retune permissions so Claude runs continuously to the next **decision gate** rather than stopping
at permission prompts.

The trade this skill manages: Claude gets a much wider grant to act, and in exchange the
obligation to stop at genuine decisions gets stronger and more explicit. Those two move together.
Widening the grant without tightening the gates produces an agent that silently makes choices that
were the user's to make — which is worse than approval fatigue, not better.

This skill rebalances three things together and keeps them consistent: the permission grant in
~/.claude/settings.json, the deny-side guardrails (rules plus the claude-danger-guard hook), and the
decision-gate contract in the toolkit policy — because widening what Claude may do without asking
is only safe if what it must stop for is widened to match.

## The insight that shapes everything here

Most permission-tuning advice says: look at what you approved, turn it into an allowlist. For this
goal that advice is close to useless, and the scanner exists to prove it with the user's own data.

Three measurements decide the design:

1. **Claude Code already runs a large built-in read-only set with no prompt in any mode** — `ls`,
   `cat`, `echo`, `grep`, `head`, `tail`, `find` (judged by its arguments — `-exec`, `-delete`, `-fprint*` and kin prompt), `wc`, `pwd`, `cd`, `diff`, `stat`, `du`, plus
   read-only `git`. In a typical history that covers a large share of all Bash subcommands. Allow rules for
   these change nothing; they just make the settings file look busy.
2. **The command long tail is enormous.** A real history spans a great many distinct command families. An
   enumerated allowlist has to cover all of them and will still miss tomorrow's.
3. **The calls that actually prompt are mostly not command families at all.** This is the one that
   invalidates the whole allowlist approach, and it was learned the expensive way — see below.

So the leverage is not in the allow list. It is in `defaultMode`, a blanket `Bash(*)` grant, and a
deny side that is actually enforceable.

### Why an enumerated allowlist fails — a worked failure

The first version of this skill produced a careful allowlist: every command family the
scan surfaced, granted by prefix. It looked comprehensive. Replayed against real Bash calls,
a large share would still have prompted, and the user would have noticed quickly.

The residue was not missing families. It was three shapes a prefix rule cannot express:

| Shape | Why no rule fixes it |
|---|---|
| Scripts invoked by path — `/Users/…/plugins/…/task-brief`, `$SDD/scripts/review-package` | Not a command name. A rule against today's path breaks on the next plugin version bump, and `$VAR` paths only resolve at runtime |
| Exec wrappers — `npx tsx --eval …` | `Bash(npx *)` is not a narrow grant, it *is* arbitrary code execution — so granting it concedes the whole argument for enumerating |
| Long-tail one-offs | Tomorrow's tool is never in yesterday's list |

Agentic work invokes helper scripts by path constantly — that is what plugins and skills *are*.
Any allowlist mined from history will keep missing them.

The honest conclusion: if you are going to end up granting an exec wrapper anyway, the enumeration
buys you nothing but maintenance. Grant `Bash(*)` and put the safety in the hook, where it can see
the whole command.

Run the scanner before proposing anything — the specific numbers are what make the
recommendation credible, and they change as the user's work changes:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/scan_transcripts.py" --limit 100
```

It reports what is already free, what actually prompts today, which grants would hand over
arbitrary code execution, the long-tail count, destructive shapes found in real history (with the
matching line, not the first line of a multi-line command), where file writes land, and — since
2026-08-29 — the **non-Bash surface**: which tools are uncovered by any rule, and a per-MCP-server
consolidation proposal. Rules are merged across the user, project and local scopes, because a
session merges them and modelling one file makes a config look worse than it is.

## Workflow

### 1. Scan, and read the scan honestly

Run the scanner. Then look for the things that should change your recommendation:

- **Destructive shapes with zero hits** are cheap to deny — the user never does them, so a deny
  rule costs nothing and closes a real hole. Zero-hit `sudo` is the clearest example.
- **Destructive shapes with hits** need judgment, not a reflex block. `rm -rf` appearing repeatedly
  is not alarming if every instance is scoped inside a worktree; blocking it outright would break
  normal work. Read the matched lines before deciding.
- **Write targets outside the workspace** are where the real risk concentrates. Config dirs
  (`~/.claude`, `~/bin`) are legitimate and frequent. Anything under `~/Documents` or a data
  directory holding PII deserves an explicit decision.
- **Exec wrappers** (`npx`, `docker`, `sh -c`, `devbox run`) are not stripped by Claude Code's
  wrapper handling, so `Bash(npx *)` is functionally "allow arbitrary code". Grant deliberately or
  not at all.

### 2. Audit the saved approvals — the scan cannot see them

Since 2026-08-29 both scripts DO read `.claude/settings.local.json` — it is one of the three
merged scopes — so the rules in it are no longer invisible. What the tooling still cannot tell you
is whether each one *should be there*. That judgement is the point of this step, so check the file
by hand every time:

```bash
find <workspace_root> -maxdepth 4 -name settings.local.json -path "*/.claude/*"
```

**Why this matters more than it looks.** Every "yes, don't ask again" writes an allow rule here —
and an allow rule is evaluated *ahead* of the auto-mode classifier. That is the same trade
`Bash(*)` makes deliberately, except granted one keystroke at a time, mid-task, and never
reviewed. Under `defaultMode: auto` this is the **only** way protection silently disappears
without anyone editing a config file.

A workspace's `<workspace_root>/.claude/settings.local.json` can accumulate entries, some of
them outward-facing MCP writes — for example a task-tracker `create_task` call (assigns work to other
people and fires notifications), wiki/doc-creation calls (a shared knowledge base), a
task-dependency call, or a cloud-drive `copy_file`. The classifier's *External System Writes*
soft-deny names ticket creation explicitly; it simply could not fire. Others can be `Bash(...)`
entries, easily read as dead weight under `Bash(*)`. Under auto mode, which drops `Bash(*)`,
each was a live approval that skipped the classifier (corrected 2026-10-05).

Sort what you find into three buckets:

- **Read-only tools** (`*_search`, `*_get_*`, `*_list_*`) — keep. Prompting on these is exactly the
  friction this skill exists to remove.
- **Outward-facing or state-changing tools** — anything that creates, deletes, sends, assigns, or
  writes somewhere other people can see it. Propose removing these so the classifier judges them
  again. This is a decision gate, not a cleanup: use `AskUserQuestion`.
- **`Bash(...)` entries** — under `defaultMode: auto` these are live, not redundant: auto mode
  drops `Bash(*)`, so each narrow rule approves its command ahead of the classifier with no
  semantic review. Keep read-only and test commands; anything that writes, deletes or reaches
  outward goes in the bucket above, as a decision gate. Outside auto mode they are redundant
  under `Bash(*)` — remove as noise.

Back the file up before editing; a workspace root is often not a git repo, and then there is no history
to recover from.

Do **not** reflexively answer this with new `ask` rules for outward-facing MCP tools. Asked once,
one user declined them for a proposal tool and a chat tool: the classifier's *Real-World
Transactions* and *External System Writes* soft-denies already cover those, and they would rather
stop preempting the classifier than stack deterministic rules on top of it. Offer the option,
respect the answer, and note the one argument on the other side — `deny → ask → allow` ordering
means an `ask` rule survives a future "don't ask again", where classifier judgment does not.

### 3. The MCP surface — one rule per server, not one per tool

Everything above this step is about Bash, and until 2026-08-29 so was every
line of the tooling. `scan_transcripts.py` filtered tool calls to
`name == "Bash"` and discarded the rest; `replay_permissions.py` modelled Bash
alone. So a config could replay at a clean **0%** while every call to a
newly-connected MCP server prompted, and nothing in the skill would say so.

That blind spot has a signature, and it is worth recognising in someone else's
config before they describe the symptom: **a long list of `mcp__server__tool`
rules, added one at a time.** Across the three settings files this
can reach dozens of rules, most of them for a single task-tracker server. That is
the enumerated-allowlist failure from the top of this file, re-run on a
different tool surface, and it grows by one rule per approval forever.

**A rule naming only a server matches every tool on that server.** Both of
these grant the whole server, and neither is exotic:

```jsonc
"mcp__tracker"        // whole server
"mcp__tracker__*"     // identical in effect
```

This is a port of Claude Code's own matcher, read out of the 2.1.246 binary
rather than the prose docs, which do not describe it. `permission_rules.py`
carries the original alongside the port. The consequence: **one rule replaces
forty-five**, and connecting a new MCP server costs one line rather than a
prompt per tool for a week.

Two things to be careful about, both of which the scanner now prints:

- **A whole-server grant sweeps in the destructive tools too.** A task
  tracker's whole-server rule, for example, would newly
  auto-allow every tool it exposes, including `delete_task` and `delete_comment`. The
  answer is not to go back to enumerating — it is `deny` (or `ask`) on the few
  that matter, which is evaluated ahead of `allow`:

  ```jsonc
  "allow": ["mcp__tracker"],
  "deny":  ["mcp__tracker__delete_task",
            "mcp__tracker__delete_comment"]
  ```

  Same shape as `Bash(*)` plus a deny side: grant broadly, carve narrowly.
- **The scanner's "destructive" list is a keyword heuristic on the tool name,
  not a classifier.** It exists to prompt judgement. A dangerous tool whose
  name lacks the keyword will not appear in it, and it can only ever name tools
  that were *actually called* — a server-wide grant also covers everything on
  that server nobody has touched yet. Read the server's full tool list before
  granting it whole, and treat the heuristic as a starting point.

**Partial patterns (`mcp__server__get_*`) — two facts, kept separate.** Claude
Code's rule *validator* accepts a tool-position glob in an allow rule and offers
`mcp__github__get_*` as one of its own examples, so such a rule is **legal**.
The matcher behaviour is now documented too: allow rules accept tool-name globs
after a literal `mcp__<server>__` prefix (`mcp__github__get_*` matches its `get_`
tools), and unanchored `*` / `mcp__*` allow globs are skipped with a warning.

The scripts *model* a partial pattern a human already wrote — so a hand-written
rule is never falsely reported as prompting — but they never **generate** one,
because the whole-server form is simpler.

An earlier version of this section claimed the whole-server forms "are the only
two Claude Code's own error text suggests." **That was false** and a blinded
review caught it: the claim was read off a narrower error about parentheses and
over-generalised, and the binary's own examples contradict it. Worth recording
because the failure was citing a source as evidence without grepping it.

One honesty constraint that governs how any of this gets reported: **transcripts
record rejections but not approvals.** A prompt the user approved is
indistinguishable from a call that never prompted, so neither script can count
the prompts someone actually saw, and neither claims to. They report which calls
are *uncovered by any rule* — which is better evidence anyway, because it also
catches the tools approved once and long forgotten. Under `defaultMode: auto`
the classifier then judges anything uncovered and may well allow it silently.
Never present the uncovered count as a tally of interruptions.

### 4. Set the mode, not the list

`defaultMode` does the work an allowlist cannot:

| Mode | What it buys | Cost |
|---|---|---|
| `acceptEdits` | File edits plus `mkdir`/`touch`/`mv`/`cp` inside the working directory stop prompting | Bash still prompts outside the built-in read-only set |
| `auto` | A semantic classifier approves routine/local/reversible work and soft-denies destructive or outward-facing actions by judgment rather than pattern | A classifier call per action; approvals vary with judgment rather than being deterministic |
| `bypassPermissions` | Nothing prompts except explicit `ask` rules (they still prompt in every mode; allow rules have no effect here) | The deny list becomes the only boundary, and `.git`/`.claude` writes stop prompting too |

`acceptEdits` is the right default for someone working in a git repo with backups: it removes the
highest-frequency interruption (edits) while leaving Bash a place where rules and hooks still
apply. Reach for `bypassPermissions` only in a container or VM.

**If the user already runs `defaultMode: "auto"`, it stays.** Do not propose changing it. Read
`claude auto-mode defaults` before reasoning about what still prompts — the classifier's soft-deny
list already covers most shapes this skill's hook blocks (irreversible local destruction,
`curl | bash`, force push, credential leakage).

The consequence, corrected 2026-10-04 from the docs (code.claude.com/docs/en/permission-modes;
the 2026-07-31 premise that `Bash(*)` leaves the hook as the only Bash boundary was wrong): on
entering auto mode Claude Code drops broad allow rules such as `Bash(*)` and restores them on
leaving. Under `defaultMode: auto`, a Bash command that no surviving rule resolves and that is not
built-in read-only goes to the classifier, with the `claude-danger-guard` hook running ahead of
all of it as a deterministic first layer. The guard is NOT the only boundary there. But rules resolve
first, so a surviving narrow allow rule (`Bash(npm test)`, or anything saved by "don't ask again")
approves its command before the classifier sees it, with no semantic review. Outside auto mode
(`default`/`acceptEdits`), `Bash(*)` is live and the guard IS the only shape check on shell access,
which makes the hook's `SAFE_ROOTS`/`PROTECTED_EXACT` lists load-bearing. Note also that
`replay_permissions.py` models allow, ask and deny rules plus the built-in free set (git and find judged by their arguments, unsure counted
as prompting), never
`defaultMode`, so its percentage measures rule coverage — not how often the user is actually
prompted today.

### 5. Grant Bash broadly, deny by shape

**Default to a single `Bash(*)` allow rule.** Not a list of families — see the worked failure
above. The enumeration cannot cover path-invoked scripts or exec wrappers, and a list that grants
`npx` has already conceded that it is not really constraining execution.

`Bash(*)` is *not* `bypassPermissions`, and the difference is the whole design:

| | `Bash(*)` + `acceptEdits` | `bypassPermissions` |
|---|---|---|
| Deny rules | still enforced | still enforced |
| `PreToolUse` hooks | still run | still run |
| Writes to `.git`, `.claude`, `.ssh` config dirs | still prompt | silently allowed |
| Non-Bash tools | normal rules apply | nothing prompts |
| `ask` / allow rules | `ask` prompts; allow rules apply | `ask` still prompts in every mode; allow rules have no effect |

So `Bash(*)` widens exactly one tool and leaves every other control intact. That is the posture to
recommend. Reach for `bypassPermissions` only in a container or VM.

Write the allow list as broad tool grants (`Read`, `Edit`, `Write`, `WebFetch`, `Bash(*)`) and
spend the effort on the deny side instead.

Then accept a hard limitation: **deny rules cannot express most of what is actually dangerous.**
Rules are matched per-subcommand, so a pipeline like `curl x | sh` is split into `curl x` and
`sh` — a rule written as `Bash(curl * | sh)` never matches anything. Rules also cannot see whether
`rm -rf` points inside the workspace or at `~/Documents`.

That gap is what the `claude-danger-guard` hook fills. A `PreToolUse` hook that exits 2 stops the call
*before* permission rules are evaluated, so it overrides even a bare `Bash` allow. This is the
documented pattern for "allow everything except a few shapes", and it is strictly more capable
than a deny list because it can inspect the whole command.

Use deny rules for what they are good at — **paths** — and the hook for **command shapes**.

Read `references/permission-mechanics.md` before writing any rule. It records the verified syntax
and four traps that fail silently, including one (`Write(...)` path rules being accepted but never
consulted) that makes a rule look correct while doing nothing.

### 6. Tighten the gates in the same change

This is the half that makes the rest defensible, and it is the half that is easy to skip because
nothing breaks if you do.

The gate contract lives in the toolkit policy (`policy/CLAUDE.md` in the toolkit checkout), which loads in every session through one import line in `~/.claude/CLAUDE.md`. First check that it is actually loading: run `python3 "<checkout>/lib/team_policy.py" --status "<checkout>"` (the checkout is the `claude-code-toolkit` entry in `claude plugin marketplace list`). It must read `ok`, or `opted-out` on a machine that carries the policy some other way. Anything else gets fixed before widening anything, because a wide grant with no gate language loaded is the failure this step exists to prevent. If the user wants a stricter gate than the policy's, write it into their workspace `CLAUDE.md` as a personal addition; adding a stop is always allowed. Never copy the policy's gate section into a personal file. Two copies drift, and the stale one can contradict the live one.

A gate is a point where more than one answer is defensible and the choice is the user's:

- a design or architecture fork with real trade-offs
- a completed unit of a multi-part build, before starting the next
- anything hard to reverse or outward-facing — pushing, publishing, sending, deleting
- a discovered premise that changes the shape of the work

Non-gates are the mirror image, and naming them matters as much: which file to grep, what to name
a variable, whether to run the tests, how to structure a commit. Stopping for those is the failure
mode that made the permissions restrictive in the first place.

Name `AskUserQuestion` as the mechanism. "Check in with the user" is vague enough that it degrades
into narrating options mid-paragraph; a specific tool with structured options does not drift.

Note honestly that CLAUDE.md shapes what Claude *tries*, while permission rules and hooks control
what Claude *may do*. The gate contract is guidance, not a boundary. It works because the model is
good at honouring it, not because it is enforced — and that is exactly why the deny side has to
hold independently.

### 7. Verify before claiming it works

A permissions change that silently fails is worse than none, because it is invisible until
something goes wrong.

**Replay the proposed config against real history first.** This is the step whose absence lets a
mined allowlist ship while still prompting often. Inspection cannot answer "will this actually stop the
interruptions" — only replay can:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/replay_permissions.py" --verbose              # merged scopes
python3 "${CLAUDE_SKILL_DIR}/scripts/replay_permissions.py" --settings /tmp/proposed.json
```

It reports the percentage of real calls that would still prompt and why, and exits non-zero above
5%. That percentage counts the prompts the config failed to remove: calls matching an `ask` rule
prompt by your choice and are listed apart under PROMPTS BY DESIGN, outside the percentage, and
calls matching a `deny` rule are blocked rather than prompted and listed under DENIED. Read both
lists before calling a config autonomous; a carve-out on a tool you use hourly is still friction. Anything above that threshold means the config does not deliver autonomy no matter how
thorough its rule list looks. Read the *reasons*: a residue of `path-invoked` or `exec-wrapper`
is proof that no amount of additional enumeration will help.

Three things about this command changed with the MCP work, and each exists because the old
behaviour could produce a confidently green number that meant nothing:

- **It replays every tool, not just Bash**, and folds them into one total. A separate non-Bash
  figure would have been read as a footnote to a green headline. `--bash-only` restores the old
  behaviour, and running both is the fastest way to show someone the gap: a config can
  read 0% Bash-only while the full replay reports the MCP residue.
- **A bare run merges all three settings scopes; `--settings <file>` models that file alone.**
  That asymmetry is deliberate — a proposal has to stand on its own terms rather than silently
  borrowing the live config's grants and looking better than it is.
- **Calls to genuinely unknown built-ins are reported but NOT counted.** Claude Code exempts many
  built-ins from prompting, it declares that internally as `isReadOnly`, and the declaration could
  not be read out of the binary. Guessing would have charged `SendMessage` and `Agent` calls that
  almost certainly never prompted — an early cut did exactly that and reported a prompting rate on nothing.

  **"Unknown" is narrower than "not MCP", and getting that wrong was a blocking defect.** The
  first cut excused every non-MCP tool, so an uncovered `Edit`, `Write` or `WebFetch` vanished
  from the gate number — on a baseline-shaped config, every file and network call was struck
  out while the headline read 0%. Those tools' status is documented in this repo, so they are
  charged. Only a tool nothing documents is excused.

The scripts carry their own suites. Run them after touching either:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/tests/test_permission_rules.py"      # the matcher port
python3 "${CLAUDE_SKILL_DIR}/scripts/tests/test_scripts_end_to_end.py"    # both scripts as subprocesses
```

Then confirm all four mechanical checks:

```bash
python3 -c "import json;json.load(open('$HOME/.claude/settings.json'))" && echo "user settings parse OK"
python3 -c "import json,sys;json.load(open(sys.argv[1]))" "<workspace_root>/.claude/settings.local.json" && echo "local settings parse OK"
python3 "<checkout>/tools/danger-guard/tests/test_guard.py"   # the guard's behavioural suite; expect OK
# Rule warnings. The canary proves the check can see a warning; the exit code proves claude ran.
claude -p 'reply ok' --model haiku --settings '{"permissions":{"allow":["Write(/tmp/zz-canary)"]}}' </dev/null 2>&1 \
  | grep -q "not matched by file permission" && echo "canary warned OK" || echo "CHECK BROKEN: the canary rule raised no warning"
out=$(claude -p 'reply ok' --model haiku </dev/null 2>&1); rc=$?
if [ "$rc" -ne 0 ]; then echo "CHECK FAILED: claude exited $rc"; printf '%s\n' "$out" | tail -5
elif printf '%s\n' "$out" | grep -i "not matched by file permission\|invalid\|ignor.*permission"; then echo "RULE WARNINGS ABOVE"
else echo "no rule warnings"; fi
```

`<checkout>` is the toolkit checkout, as in step 6. The guard and its suite live there, in
`tools/danger-guard/`; this skill carries no copy of its own.

The last check matters most: Claude Code warns at startup about path rules it accepted but will
never consult. Silence there is the evidence the rules are live, but only once the canary has
warned and `claude` has exited 0. A `claude` that is missing or fails also prints nothing, and so
does a release that rewords the warning. **Report "no rule warnings" only when the block printed
both `canary warned OK` and `no rule warnings`.** `CHECK BROKEN` or `CHECK FAILED` means the check
did not run, which is not a pass. An untrusted workspace prints `Ignoring N permissions.allow
entry … not been trusted` instead and drops every rule in its project settings, so that line counts
as a warning too. `--debug` adds nothing here: the warning prints without it. Run it from the
workspace root, so it reads every settings scope that applies there. It is **not** a syntax
check: a malformed `settings.local.json` drew no output at all in a probe, so a well-formed file is
proven by the parse lines above.

If the full suite is unavailable, the two-line smoke test still beats nothing:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"sudo rm -rf /"}}' | "<checkout>/tools/danger-guard/claude-danger-guard"; echo "blocked -> exit $?"
echo '{"tool_name":"Bash","tool_input":{"command":"git status"}}' | "<checkout>/tools/danger-guard/claude-danger-guard"; echo "allowed -> exit $?"
```

**Finally, tell the user to restart their other sessions.** Permissions are read at session start,
so a long-running or resumed session keeps whatever it booted with. A long-lived
session can hold stale permissions for a long time. Without a restart the user will report the change
"didn't work" and both of you will debug the wrong thing.

## What this skill will not do

- **Write a settings file before the user approves that exact change.** Show the proposed
  rules and their replay result, then ask with `AskUserQuestion`. Installing or invoking this
  skill authorizes no permission change by itself.
- **Grant `bypassPermissions` on a normal workstation.** The deny list becomes the only boundary,
  and a deny list cannot express command shapes. Recommend `acceptEdits` plus the hook instead,
  and say why.
- **Write allow rules for the built-in read-only set.** They are noise that makes the change look
  bigger than it is.
- **Treat the scanner as a complete picture.** It is better than it was — it now reads every tool
  and merges all three settings scopes — but it still cannot see the `mcpServerPolicy` rules an
  HTTP or SSE server can supply for its own tools (`always_allow`/`always_ask`/`always_deny`),
  which Claude Code merges in at runtime and which appear in no settings file. Step 2 also still
  matters: a run that stops at the green number can miss a real finding.

  The historical version of this warning is worth keeping, because it is what the change on
  2026-08-29 was fixing: both scripts read **Bash calls and one settings file** and nothing else,
  so a clean 0% replay said the Bash grant was working and said nothing whatsoever about MCP.
- **Enumerate MCP tools one rule at a time.** Same failure as the Bash allowlist, same reasoning,
  and the fix is simpler here because Claude Code already supports the whole-server form. If a
  config has more than a handful of `mcp__server__tool` rules, that is the finding.
- **Generate a partial MCP pattern (`mcp__server__get_*`).** Such a rule is legal — Claude Code's
  validator offers one as an example, and the docs now state the matcher globs it after a literal
  `mcp__<server>__` prefix — but the whole-server form is simpler and covers the case. Model one a
  human wrote; do not emit one.
- **Report the uncovered count as a number of prompts.** Transcripts do not record approvals. It
  is where a prompt *can* happen, not a tally of interruptions, and saying otherwise turns a
  model into a fabricated measurement.
- **Ship an enumerated Bash allowlist.** It cannot cover path-invoked scripts or exec wrappers, it
  needs maintenance forever, and its thoroughness is what makes the failure hard to spot. If a
  reason to enumerate seems compelling, run `replay_permissions.py` on the enumeration before
  believing it.
- **Widen permissions without the gate contract loaded.** Step 6's status check comes first. If the user only wants the permission
  half, say plainly that the trade is now one-sided and let them decide — do not quietly do half.
- **Claim the workspace is a sandbox.** File tools are scoped to the working directory, but Bash
  subprocesses are not. `Read`/`Edit` deny rules also cover Bash file commands Claude Code
  recognises (`cat`, `sed`) but not a Python script that opens the same path. Real containment
  needs OS-level sandboxing; anything less should be described as a speed bump.
