# Filling in their TOOLING.md

Read by `/toolkit-core:onboard` at Phase 5b, after the template is copied to
`<workspace_root>/TOOLING.md`. **Ask them nothing here.** Almost every `TODO` in the file is
observable, and guessing is not the failure mode: asking is.

## Observe, don't ask

Run these and write what you see. Each is optional: a command that is not installed is itself a
fact worth a row.

```bash
for c in gh git python3 node jq uv rg; do printf "%-8s %s\n" "$c" "$(command -v "$c" || echo MISSING)"; done
python3 -V; git --version
command -v gh >/dev/null && gh auth status 2>&1 | head -3
ls -d ~/Library/CloudStorage/* ~/Library/Mobile\ Documents 2>/dev/null
```

If they use a password manager or a cloud provider's CLI, check whether it is installed and signed
in the same way (`command -v <name>`, then its own status command). Record only that it exists and
how it is signed in, never a secret, a token or a vault item.

## The three parts that carry most of the value

- **Connectors.** List what is live, and, the important half, what they connected but have not
  authorized. An unauthorized connector fails in a way that looks like "no data" rather than "not
  signed in", so it gets misdiagnosed every time.
- **What is NOT installed.** A missing-tool line saves more time than a present-tool line, because
  it stops a search that cannot succeed.
- **The digest block.** Rewrite it for them and keep it under about 25 lines: it is shown at every
  session start, so every line is paid for in every session. Delete the `TODO` notes inside it.

Flag any personal account with production reach (a signed-in `gh`, a cloud or hosting CLI) with ⚠️
in its row. Don't make it a problem to solve today; make it visible.

Delete the template's header block once the file is theirs.
