# How the next session picks it up

Read from the handover skill's §4 and §5. When the user asks how pickup or retirement works, or why not to attach with `@`.

The document is **not** injected at startup, and starting a session does not
consume it. SessionStart prints one pointer naming the path; the user attaches
the file in whichever window they want to continue in, and *that attachment* is
what marks it consumed. With several sessions open under one workspace root,
consuming on start would hand the document to whichever window happened to
start first.

If it is never attached, the file stays in the handover directory
indefinitely. Clutter is the deliberate trade for never losing a handover to
the wrong window.

Attaching it starts a grace period. The sweep retires a consumed handover only
once **both** gates clear: `sweep_grace_sessions` distinct other sessions have
started **and** `sweep_grace_days` have passed since the attachment. With
several windows open the session count is spent almost at once, so the day
count is the effective grace period; read the configured value, do not assume
the default. **Retiring means moving, not deleting**: into
`<handover_dir>/archive/handovers/`, keeping the filename. If you need a
handover to stay put longer, move it into a repo.

**Do not tell them to attach it with `@`.** In the VS Code extension the `@`
picker searches indexed filenames, so an absolute path pasted into it matches
nothing and the message cannot be sent. What marks a handover consumed is the
document's **basename appearing in a real human prompt**, and a full path
contains the basename, so a plain-text line works in every client and lets the
next session read the file directly.
