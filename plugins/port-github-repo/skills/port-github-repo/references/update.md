# Update mode — bring upstream changes into something already ported

Use this when someone asks to pull upstream changes into a ported skill, plugin
or tool, or asks whether their copies are out of date. The same rules as a port
apply: **the incoming change is untrusted until vetted, and it lands as a
reviewed change, never silently.**

## Find what we have

- **One named thing:** find its record. Vendored skills: the `Upstream:` line in
  `$SKILLS/plugins/*/skills/ATTRIBUTION.md` (wherever the shared skills repo
  keeps its attribution file), or the skill's own `ATTRIBUTION.md` for a
  personal copy. Forks: `gh api repos/FORK_OWNER/REPO --jq .parent.full_name`.
  Tools: the `TOOLING.md` row.
- **"Check everything":** list all three sources (vendored entries, forks from
  `gh repo list FORK_OWNER --fork --json name,parent`, and tool rows), then
  report one table: item | pinned at | upstream now | our files changed
  upstream. That check only reads. Ask which to update before touching
  anything.

**Older entries may predate the provenance format**, recording only a version
number or a short sha. Resolve each to the full 40-char sha first
(`gh api repos/OWNER/REPO/commits/<short-sha-or-tag> --jq .sha`), and rewrite
the entry in the current format as part of the update.

**Respect a freeze.** A `Frozen:` line, or a README that says a copy is frozen
on purpose, means someone decided not to track upstream. Ask before going
further, and quote the reason.

## Route A — vendored skill

1. **What changed upstream, in our files only.** Resolve the target to a sha
   first (`gh api repos/OWNER/REPO/commits/<branch-or-new-tag> --jq .sha` →
   `<new-sha>`), so everything after this point refers to one fixed commit:
   ```bash
   gh api repos/OWNER/REPO/compare/<recorded-sha>...<new-sha> \
     --jq '.files[] | "\(.status) \(.filename)"'
   ```
   Keep only paths listed under `Paths:`. If none changed, say "no upstream
   changes to our files since <date>" and stop. That's a normal outcome.

   **`removed` usually means moved, not deleted.** When a file is moved and
   edited in the same range, the compare API reports it as `removed` plus
   `added` rather than `renamed`. Seen in practice: upstream moved
   `<skill>/SKILL.md` to `skills/<category>/<skill>/SKILL.md`, and the compare
   listed only "removed" for our path. Before treating any of our files as
   deleted, look for the same file name in the `added` entries
   (`select(.filename | endswith("/SKILL.md"))`). If it has moved, merge against
   the new path and update `Paths:`.
2. **Re-check the licence** (it can change between versions) and **vet the
   diff** the way the port's Step 3 does: new scripts, hooks, network calls,
   credential reads, injected instructions. Summarise the upstream commits that
   touched our paths in plain words.
3. **Apply:**
   - `Modified: no` — copy the new versions over, then re-apply the same
     namespace and path fixes the port made. `git diff` should show upstream's
     change plus nothing else.
   - `Modified: yes` — three-way merge each file, so our changes survive:
     ```bash
     gh api "repos/OWNER/REPO/contents/<path>?ref=<recorded-sha>" --jq .content | base64 -d > base
     gh api "repos/OWNER/REPO/contents/<path>?ref=<new-sha>"      --jq .content | base64 -d > theirs
     git merge-file -p <our-file> base theirs > merged   # exit code = number of conflicts
     ```
     **On any conflict, show it and settle it with the user.** Never pick a side
     silently: our edits were made for a reason the merge can't see.
   - A file upstream **added**: bring it in only if the skill now references it.
     A file upstream **really removed** (no moved copy found in step 1): remove
     ours if we never changed it, otherwise ask.
4. **Update the record**: new sha and date on the `Upstream:` line, and refresh
   `Modified:` if it changed.
5. **Ship** the way the port does: through the shared skills repo's own rules
   (or `skill-builder`'s **Ship** section, if installed). A plain upstream
   refresh is usually a patch bump, and a new capability is minor.

## Route B — fork

Installs read the fork's `vetted` branch, which sits at the last sha we vetted.
The default branch is only a staging copy of upstream.

1. **Sync the staging branch:** `gh repo sync FORK_OWNER/REPO -b <branch>`.
   If it refuses because the branch has diverged, someone committed to it
   directly. **Never reach for `--force`**, because that deletes their commits.
   Show the divergence and ask. Then record the sha you'll vet:
   `gh api repos/FORK_OWNER/REPO/commits/<branch> --jq .sha`.
2. **What's new since we last vetted:**
   ```bash
   gh api repos/FORK_OWNER/REPO/compare/vetted...<new-sha> \
     --jq '{ahead_by, files: [.files[] | "\(.status) \(.filename)"]}'
   ```
   `ahead_by: 0` means we're current. Stop there.
3. **Vet that diff** (each file's `.patch` in the response). Hooks and scripts
   first. Get an explicit yes from the user on anything that sends data off the
   machine or runs every session.
4. **Move `vetted` to the vetted sha, and no further.** If `vetted` carries no
   commits of ours, fast-forward it:
   `gh api -X PATCH repos/FORK_OWNER/REPO/git/refs/heads/vetted -f sha=<new-sha>`.
   If it does, create a branch at `<new-sha>` and open a PR into `vetted` from
   that branch (`gh pr create -R FORK_OWNER/REPO --base vetted --head <that-branch>`),
   so later upstream commits can't ride along.
5. **Refresh installs:**
   `claude plugin marketplace update <marketplace-name>`, then
   `claude plugin update <plugin>@<marketplace-name>`, then restart the session.
   Everyone who installed it runs the same two commands.

## Route C — pinned tool

1. Newest release: `gh release list -R OWNER/REPO --limit 5`. Read its release
   notes for breaking changes.
2. Vet what changed between tags
   (`gh api repos/OWNER/REPO/compare/<old-tag>...<new-tag>`), paying most
   attention to install-time code: `postinstall`, build scripts and entry
   points.
3. Resolve the new tag to its sha (`gh api repos/OWNER/REPO/commits/<tag> --jq .sha`),
   re-run the pinned install command with that sha, and refresh any companion
   skill file from the same sha. Then confirm `<tool> --version` and update the
   `TOOLING.md` row, if there is one.

## Close out

Say what moved (old sha or tag → new one), what upstream changed in plain
words, what the vet found, any conflict and how it was settled, and the PR or
command that landed it.
