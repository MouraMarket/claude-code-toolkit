---
name: caffeinate
description: Keep this Mac awake on battery while Claude works, or turn that back off
argument-hint: "[on | off | status]"
allowed-tools: Bash
---

The mode is what was typed after `/caffeinate`, quoted here: "$ARGUMENTS".
When those quotes are empty, the mode is `status`. Run, with the mode in place
of `<mode>`:

```
~/.local/bin/claude-caffeinate <mode>
```

Never run it with no mode at all: that prints nothing and exits 0, which reads as
success while reporting nothing.

Accept only `on`, `off` and `status`. Anything else — ask which was meant rather
than guessing; the wrong one of `on`/`off` is a machine-wide config change in the
wrong direction.

**Do not auto-execute this via a `!` block.** Every other command in this repo
runs its tool the same way, through the Bash tool, and that is deliberate here:
a shell default such as `${…:-status}` reads naturally and is wrong twice over.
Claude Code substitutes the plain argument placeholder, not that form, and the
argument is not an environment variable, so a shell would expand the whole thing
to `status`. That turns `/caffeinate on` into a status read that writes nothing
while printing something reassuring.

## Reporting the result

Print what the command said, verbatim, in your reply, in a code block, and stop.
The person does not see tool output, so "see above" or a summary hides it. It is
written for a human — do not restate it, summarise it, or add commentary.

Then, **only if it applies**, add ONE of these:

- **The command exited non-zero.** Say what failed. The usual cause is a config
  file that is not valid JSON, and the fix is to repair
  `~/.config/claude-code-toolkit/config.json` by hand. Do NOT offer to rewrite
  that file yourself — it holds every other tool's settings.
- **`status` shows a hold that is NOT bound to a process.** Say it will end on
  its ceiling rather than when the session exits, and that the next turn in that
  session re-binds it. Normal right after an upgrade.

The below-floor case needs no instruction here: `on` reads the power state and
prints that warning itself, because a rule that depends on the model noticing
something the output never mentions is a rule that never fires.

## What this actually changes

`on` makes every session on this machine hold a sleep assertion on battery, not
just on wall power. It is a machine-wide, persistent config change — it outlives
the session that ran it and survives restarts, until someone runs `off`.

**`on` lapses on its own after 24 hours with no battery use** — so a setting
turned on for one long build and forgotten cannot hold the machine awake for a
week. Working unplugged restarts that clock; working plugged in does not, because
that is not using the override. A lapse also releases holds already running, not
just future ones. `battery_override_hours: 0` makes it permanent.

`off` returns to holding **only on wall power**. It does not disable the feature;
plugged-in behaviour is unaffected either way. Turning it off completely means
setting `caffeinate.enabled` to `false` in the config by hand, which is a bigger
decision than this command makes.

If `status` reports the override **LAPSED**, say so plainly and offer
`/caffeinate on` to restart it. That state is worth naming because `config.json`
still reads `require_ac: false` while nothing is being held — the file and the
behaviour genuinely disagree, and this output is the only place that is visible.

Running sessions pick either change up on their next turn. No restart is needed,
because each hook process reads the config fresh.

## What it cannot do

**Closing the lid still sleeps the Mac.** Clamshell sleep ignores every
`caffeinate` flag; only `sudo pmset disablesleep 1` overrides it, and nothing
here will touch that. If asked for the lid case, say that is what it would take
and let the human decide — do not run it for them.

The display still goes dark and the screensaver still starts. That is deliberate:
processes run perfectly well behind both, and keeping the screen lit to finish a
build is a different and worse request.

## How long it holds

The point is to let Claude's **background work finish** with nobody at the
keyboard. Two kinds of hold do that:

- **Session hold — a 30-minute idle timer.** A prompt or a tool batch re-arms it
  (subagents' batches included), at most every 2 minutes, so the Mac sleeps
  **28–30 minutes** after the last activity. With several sessions open, it runs
  from the last activity in any of them.
- **Job hold — one per background job, capped at 1 hour.** A command Claude
  started with `run_in_background` (a Codex gate, a test run) gets its own hold
  that ends the moment the job finishes. The 1-hour cap ends it anyway if the job
  hangs; a job is never re-held after its cap.

The idle timer is the fallback for work that is running but was not detected.
Both numbers are the maintainer's choice; `hold_seconds` and `job_hold_seconds`
change them. Do not propose removing either deadline — a hold with no deadline is
how a hung job keeps a Mac awake overnight.

**Every hold also ends** when its session's `claude` process exits, when the
battery falls to the floor (20% by default, on battery only), when you unplug
without the battery override, or when the override lapses.
