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

A hold ends when its session's `claude` process exits, when the battery falls to
the floor (20% by default), when the battery override lapses, or after two hours
with no activity at all — whichever comes first.

That two-hour ceiling is a **hang detector, not an idle timer**: every prompt and
every tool batch resets it, *including batches inside dispatched subagents*. So a
session waiting on healthy subagents never approaches it, and one waiting on a
subagent that hung goes quiet and sleeps. Do not propose removing it — that was
raised on 2026-08-26 and rejected, because removing it deletes exactly that
protection.
