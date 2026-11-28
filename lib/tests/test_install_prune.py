#!/usr/bin/env python3
"""Tests for install.sh's removal of links to files the repo no longer has.

**Why this file exists at all: `install.sh` had no test of any kind**, which is
how a retired tool came to leave its symlink behind forever. A tool added and
then reverted is the case that exposed it — the only cleanup loop
keyed off `EXECUTABLE_NAMES`, and retiring a tool is precisely when a name
leaves that list, so cleanup stopped happening at the one moment it was needed.
`--check` reported clean over the leftovers.

This drives **the real `install.sh`**, not a re-implementation of its logic. A
test that re-implemented the prune would pass while the installer did something
else entirely, which is the defect class this repo keeps writing down.

The sandbox is a full copy of the repo with every `tests/` directory stripped,
so the installer's own Tests section finds nothing and this cannot recurse into
itself. `HOME` is redirected, so nothing here touches the real machine — and
that is load-bearing, because a bug in the code under test DELETES FILES.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import install_fixture as fx

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def _bash_available():
    return shutil.which("bash") is not None


class InstallSandbox(unittest.TestCase):
    """A throwaway HOME plus a throwaway copy of the repo."""

    def setUp(self):
        if not _bash_available():
            self.skipTest("bash not available")
        if not os.path.isfile(os.path.join(REPO, "install.sh")):
            self.skipTest("install.sh not found next to lib/")

        self.tmp = tempfile.mkdtemp(prefix="install-prune-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "repo")

        # Strip `tests` so the installer's suite runner finds nothing: without
        # this the test runs the whole toolkit suite inside itself.
        # load_guard_lib.py is left out so the shim step has nothing to import and
        # stands down: these suites assert a clean or exact --check, and which
        # shims it proposes would depend on which heavy tools the machine has on PATH.
        # The shim step has its own suite (test_install_load_guard_shims.py).
        shutil.copytree(
            REPO, self.repo,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".claude", "tests",
                                          "__pycache__", "load_guard_lib.py"))
        # Hook executables and profile skill dirs that later port rows ship.
        fx.stub_pending_components(self.repo)
        os.makedirs(self.home + "/.local/bin", exist_ok=True)

    def run_install(self, *args):
        env = dict(os.environ)
        # Hermetic AND safe needs both levers: install.sh reads
        # CLAUDE_TOOLKIT_CONFIG when it is set and otherwise falls back to
        # $HOME/.config/claude-code-toolkit/config.json, so the redirected HOME
        # alone would still follow an exported config.
        env.pop("CLAUDE_TOOLKIT_CONFIG", None)
        env["HOME"] = self.home
        env = fx.shim_env_for_home(env, self.home)
        if "--check" in args:
            fx.seed_home_installed(self.home, self.repo)
            # --check exits 1 on an absent link; this is a healthy machine.
            fx.install_links(self.repo, self.home)
        proc = subprocess.run(
            ["bash", os.path.join(self.repo, "install.sh")] + list(args),
            capture_output=True, text=True, env=env, cwd=self.repo,
            # stdin=DEVNULL is REQUIRED, not tidiness. `capture_output` only
            # redirects stdout/stderr, so stdin is INHERITED. The sandbox HOME
            # has no config, install.sh therefore runs first-time setup, and
            # setup_config prompts whenever `sys.stdin.isatty()`. Run from a
            # real terminal — which is how a human runs the suite, and how this
            # repo's own documented `CLAUDE_TOOLKIT_CONFIG=… python3 tests/…`
            # line runs it — the prompt goes into the captured pipe and the
            # test blocks forever, silently eating the user's keystrokes.
            # Without a TTY it finishes; with one it blocks.
            stdin=subprocess.DEVNULL,
            # A hang must fail, not wait. If stdin ever leaks back in, this
            # turns an indefinite block into a legible failure.
            timeout=600)
        return proc

    # -- fixtures ---------------------------------------------------------

    def link(self, rel_under_home, target):
        path = os.path.join(self.home, rel_under_home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.lexists(path):
            os.remove(path)
        os.symlink(target, path)
        return path

    def dangling_into_repo(self, rel_under_home):
        """A link we plainly created, whose target the repo no longer has.

        NAME THE ~/bin FIXTURES `claude-retired`, matching this target's own
        basename. The `~/bin` loop prunes only a link whose basename equals its
        target's — that is what stands in for "one we created" now that it
        walks the directory instead of iterating a name array, and a
        mismatched fixture there is a HAND-MADE ALIAS, which the loop is
        supposed to leave alone. Three tests failed exactly that way when the
        basename test landed: the fixture, not the code, was unrealistic.
        `~/.local/bin` fixtures have no such constraint.
        """
        return self.link(rel_under_home,
                         os.path.join(self.repo, "tools/retired/claude-retired"))

    def exists(self, path):
        return os.path.lexists(path)

    def managed_but_gone(self, rel_under_home, repo_rel):
        """A link the installer still manages, whose file this checkout lacks.

        An older branch, a bisect, a rename in flight. The machine
        has the link from a previous install; the tree being installed from does
        not have the file. `link` reports this "missing in repo, skipped" and
        deliberately leaves the link alone, so the prune must not contradict it.
        """
        target = os.path.join(self.repo, repo_rel)
        self.assertTrue(os.path.exists(target),
                        "fixture is stale: %s is not in the repo, so this test "
                        "would prove nothing about a LIVE tool" % repo_rel)
        # `skills/autonomy-permissions` is a DIRECTORY symlink target, and `-L`
        # / `-e` behave the same on those — so the fixture has to handle both
        # shapes or it can only ever test executables.
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        self.unregister_hook_exe(os.path.basename(repo_rel))
        return self.link(rel_under_home, target)

    def unregister_hook_exe(self, exe):
        """Drop the removed tool's handlers from this fixture's plugin manifest.

        A registered hook whose executable is gone is a machine whose hooks are
        NOT protected, and install.sh now exits 1 on that. These cases are about
        the prune, not about that verdict, so the fixture models a checkout
        whose manifest does not register the tool it lacks."""
        path = os.path.join(self.repo, "plugin", "hooks", "hooks.json")
        with open(path) as handle:
            data = json.load(handle)
        hooks = {}
        for event, blocks in data["hooks"].items():
            kept = []
            for block in blocks:
                block = dict(block)
                block["hooks"] = [h for h in block.get("hooks", [])
                                  if exe not in h.get("command", "")]
                if block["hooks"]:
                    kept.append(block)
            if kept:
                hooks[event] = kept
        data["hooks"] = hooks
        with open(path, "w") as handle:
            json.dump(data, handle, indent=2)


class TestPrunesLinksToFilesTheRepoNoLongerHas(InstallSandbox):

    def test_a_dangling_link_into_this_repo_is_removed(self):
        link = self.dangling_into_repo(".local/bin/claude-retired")
        self.assertTrue(self.exists(link), "fixture did not create the link")
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.exists(link),
                         "a link to a file the repo no longer has survived "
                         "install — this is the retired-tool leftover")

    def test_every_managed_link_directory_is_covered(self):
        # `~/.local/bin` is the only directory the installer links into or
        # prunes now; commands, agents, skills and build-lens ship in the plugin.
        paths = {
            "~/.local/bin": self.dangling_into_repo(".local/bin/claude-retired"),
        }
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for where, path in paths.items():
            self.assertFalse(self.exists(path), "%s was not pruned" % where)

    def test_check_reports_the_leftover_and_deletes_NOTHING(self):
        link = self.dangling_into_repo(".local/bin/claude-retired")
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("claude-retired", proc.stdout,
                      "--check did not report the leftover, so the "
                      "'zero changes on an installed machine' invariant would "
                      "still hide a half-uninstalled state")
        self.assertTrue(self.exists(link),
                        "--check DELETED something; it must only report")


class TestPruneDoesNotOverreach(InstallSandbox):
    """The dangerous direction. This code removes files."""

    def test_a_link_pointing_somewhere_else_is_never_touched(self):
        # Someone else's tool that happens to sit in the same directory. Not
        # ours, and a dangling target does not make it ours.
        outside = os.path.join(self.tmp, "not-our-repo", "their-tool")
        foreign = self.link(".local/bin/their-tool", outside)
        proc = self.run_install()
        # Without this, the test passes when install.sh never ran at all.
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.exists(foreign),
                        "install removed a symlink that does not point into "
                        "this repo — that is someone else's file")

    def test_a_live_link_into_this_repo_survives(self):
        self.run_install()
        for name in ("claude-model-guard", "claude-session-claim"):
            path = os.path.join(self.home, ".local/bin", name)
            self.assertTrue(self.exists(path), "%s was not installed" % name)
            self.assertTrue(os.path.exists(path),
                            "%s is present but dangling — the prune removed a "
                            "LIVE tool's target or the link itself" % name)

    def test_a_live_unmanaged_link_into_this_repo_survives(self):
        """The liveness test on its own, with nothing else protecting the link.

        The managed names above are ALSO shielded by `is_managed_link`, so
        dropping the `[[ -e ]]` test survived that case (mutation I13). A name
        the installer never links reaches the liveness test alone.
        """
        mine = self.link(".local/bin/mytool",
                         os.path.join(self.repo, "install.sh"))
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.exists(mine),
                        "install removed a LIVE link into this repo under a "
                        "name it does not manage — only dangling links are "
                        "leftovers")

    def test_a_real_file_is_not_removed_even_with_a_retired_name(self):
        """Only symlinks are ours to reason about; a regular file is the user's.

        **Honest note: no mutation of the current implementation kills this.**
        Dropping the `[[ -L ]]` guard still leaves a regular file safe, because
        `readlink` returns empty and the `"$REPO"/*` test then fails. It is kept
        deliberately as a standing invariant rather than deleted, because the
        bug it guards is nameable and plausible: a future rewrite that matches
        on NAMES (a tombstone list) instead of walking symlinks would delete
        this file, and nothing else in the suite would notice.
        """
        path = os.path.join(self.home, ".local/bin", "claude-retired")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho mine\n")
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.isfile(path),
                        "install deleted a regular file, not a symlink")


class TestTheLegacyBinPruneWalksTheDirectory(InstallSandbox):
    """The `~/bin` loop had the ORIGINAL array-keyed bug the loop above
    was rewritten to escape, and was untested until this class.

    `for name in "${EXECUTABLE_NAMES[@]}"` could only prune a name still IN
    that array, and retiring a tool is exactly when its name leaves it — so
    the one moment cleanup was needed was the one moment it stopped
    happening. Walking `~/bin` directly, keyed on nothing but the filesystem,
    fixes that.

    Unlike the loop above, this one does NOT consult `is_managed_link`: `~/bin`
    is superseded and nothing links into it any more, so a repo-pointing
    symlink there is stale regardless of whether the tool it names is still
    alive elsewhere. Protecting a "still managed" name here would defeat the
    loop's entire purpose.

    **Be precise about what is and is not pinned here, because an earlier
    version of this docstring was not and it was wrong either way it was
    read.** It claimed both that the exclusion "can never fire" AND that
    `test_an_active_tools_superseded_bin_copy_is_removed_too` would break if
    it were added back. Those cannot both be true, and mutation proved it:
    adding `is_managed_link "$old"` — a full `~/bin` path, which is what the
    exclusion would literally be — SURVIVES the whole suite, because nothing
    ever records such a path. Adding the basename-mapped variant
    (`is_managed_link "$HOME/.local/bin/${old##*/}"`) is killed by that test.

    So: the literal exclusion is inert and NO test pins it — that is a
    property of the key space, not something a fixture can assert. The
    name-mapped variant is pinned. Do not restore either sentence in its old
    form; a test file claiming coverage it does not have is the one place
    that error costs the most.

    What DOES discriminate here is basename equality: every link the
    installer made into `~/bin` named the file it pointed at, so a
    hand-made alias (`~/bin/mg -> $REPO/tools/model-guard/claude-model-guard`)
    is not ours and is left alone, while a retired tool's leftover still
    matches and is still pruned. That is keyed on the filesystem, so it does
    not readmit the array bug this class exists to cover.
    """

    def test_a_hand_made_alias_into_the_repo_is_never_removed(self):
        """The walk's blast radius past the old array, and the reason for the
        basename test.

        `for name in "${EXECUTABLE_NAMES[@]}"` could only ever look at
        `$HOME/bin/<tool-name>`, so a symlink a HUMAN put in ~/bin pointing
        into this repo under a name of their own was unreachable by it. A bare
        walk deletes those: everything past the installer's own tool names is by
        construction a link this installer never created, which contradicts
        the loop's own "one we created" promise.

        Two shapes, both measured as deleted before the basename test existed.
        """
        alias = self.link("bin/mg",
                          os.path.join(self.repo, "tools", "model-guard",
                                       "claude-model-guard"))
        installer = self.link("bin/toolkit-install",
                              os.path.join(self.repo, "install.sh"))
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.exists(alias),
                        "a hand-made ~/bin alias into this repo was deleted — "
                        "the installer never created it, so it is not ours")
        self.assertTrue(self.exists(installer),
                        "a hand-made ~/bin alias to install.sh was deleted")

    def test_a_retired_tools_bin_link_is_removed(self):
        """The bug, reproduced: a name with no `link` call anywhere and not
        in EXECUTABLE_NAMES. The array-keyed loop could never reach this.

        Note this is also the control for the basename test above: a retired
        tool's leftover DOES have matching basenames, so narrowing to
        "we plausibly created it" must not cost the walk's fix.
        """
        link = self.dangling_into_repo("bin/claude-retired")
        self.assertTrue(self.exists(link), "fixture did not create the link")
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.exists(link),
                         "a ~/bin link for a retired name survived install "
                         "— this is the leftover the walk exists for")

    def test_an_active_tools_superseded_bin_copy_is_removed_too(self):
        """`~/bin` removal is by LOCATION, not by managed status. A leftover
        for a tool that is very much alive in ~/.local/bin is exactly what
        this loop exists to clear — matching the old array-keyed loop's
        behaviour for any name still in EXECUTABLE_NAMES. The tool's REAL,
        current link is untouched: this loop only ever looks inside ~/bin.
        """
        target = os.path.join(self.repo, "tools/model-guard/claude-model-guard")
        self.assertTrue(os.path.exists(target), "fixture is stale")
        old_copy = self.link("bin/claude-model-guard", target)
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.exists(old_copy),
                         "a live tool's superseded ~/bin copy survived — "
                         "this loop removes EVERY repo-pointing ~/bin link, "
                         "not just retired ones")
        real_link = os.path.join(self.home, ".local/bin/claude-model-guard")
        self.assertTrue(os.path.exists(real_link),
                        "pruning the ~/bin leftover broke the tool's real, "
                        "current link")

    def test_a_bin_link_pointing_elsewhere_is_never_touched(self):
        outside = os.path.join(self.tmp, "not-our-repo", "their-tool")
        foreign = self.link("bin/their-tool", outside)
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.exists(foreign),
                        "install removed a ~/bin symlink that does not "
                        "point into this repo — that is someone else's file")

    def test_check_reports_the_bin_leftover_and_deletes_NOTHING(self):
        link = self.dangling_into_repo("bin/claude-retired")
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("claude-retired", proc.stdout,
                      "--check did not report the ~/bin leftover")
        self.assertTrue(self.exists(link),
                        "--check DELETED something; it must only report")

    def test_a_real_file_at_a_prunable_bin_name_is_not_removed(self):
        path = os.path.join(self.home, "bin", "claude-retired")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho mine\n")
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.isfile(path),
                        "install deleted a regular file in ~/bin, not a "
                        "symlink")

    def test_the_loop_discriminates_within_one_run(self):
        """The positive control, in the same install as the retired case.

        Without this, a loop that has simply been switched off would pass
        the retired-link test above for the wrong reason if run alone — this
        proves a foreign link survives in the SAME run a retired one is
        removed in.
        """
        retired = self.dangling_into_repo("bin/claude-retired")
        foreign = self.link("bin/their-tool",
                            os.path.join(self.tmp, "elsewhere", "their-tool"))
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.exists(retired),
                         "the retired ~/bin link was not pruned")
        self.assertTrue(self.exists(foreign),
                        "a foreign ~/bin link was wrongly pruned")


class TestPruneLeavesLinksTheInstallerStillManages(InstallSandbox):
    """"Dangles and points into $REPO" also describes a LIVE tool that is
    merely absent from the checkout `install.sh` is running from.

    Before the fix, on the real installer, one run printed
    both `missing in repo, skipped: tools/caffeinate/claude-caffeinate` and
    `removing ~/.local/bin/claude-caffeinate`, and a real apply deleted the
    link. `link` and the prune disagreed about the same condition, far apart
    in the script, and the prune won.

    **Why the class this sits next to could not see it: none of its fixtures is
    a tool the installer manages.** `TestPruneDoesNotOverreach` tests a foreign
    link, a live link, and a regular file — three ways of not being the case
    that breaks. Every test here uses a name the installer really does link, so
    the exclusion is exercised from inside the protected set rather than
    around it.
    """

    def test_a_live_tool_absent_from_this_checkout_keeps_its_link(self):
        link = self.managed_but_gone(".local/bin/claude-caffeinate",
                                     "tools/caffeinate/claude-caffeinate")
        proc = self.run_install()
        # The source is deliberately gone from this checkout: a required failure.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        # The block is printed last, so it also proves the run reached the end.
        self.assertIn("Required components failed:", proc.stdout)
        self.assertTrue(self.exists(link),
                        "the prune deleted a link to a tool the installer "
                        "still manages — it is in EXECUTABLE_NAMES and has a "
                        "link call; only this checkout lacks the file")

    def test_one_run_never_both_skips_and_removes_the_same_name(self):
        """The symptom, asserted directly rather than via the filesystem.

        Two contradictory messages about one tool is what made the original
        defect hard to read, and it is observable even where the deletion is
        not — under `--check`, for instance.
        """
        self.managed_but_gone(".local/bin/claude-caffeinate",
                             "tools/caffeinate/claude-caffeinate")
        proc = self.run_install()
        # Without this the test passes when install.sh never reached the prune:
        # the skip message comes from `link`, which is UPSTREAM of any abort, so
        # `removals == []` is then free: with `exit 9` injected after the link
        # section, most of this suite fails and this test alone would pass.
        # The source is deliberately gone from this checkout: a required failure.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        # The block is printed last, so it also proves the run reached the end.
        self.assertIn("Required components failed:", proc.stdout)
        out = proc.stdout + proc.stderr
        self.assertIn("missing in repo, skipped: tools/caffeinate/claude-caffeinate",
                      out,
                      "fixture did not reach `link`'s missing-in-repo path, so "
                      "the contradiction under test was never set up")
        removals = [line for line in out.splitlines()
                    if "removing" in line and "claude-caffeinate" in line]
        self.assertEqual(removals, [],
                         "one run reported the same tool both skipped and "
                         "removed: %r" % removals)

    def test_the_exclusion_discriminates_within_one_run(self):
        """The positive control, in the same install as the protected case.

        Without this, every test above passes against a prune that has simply
        been switched off — which is a strictly worse defect than the one being
        fixed, and looks identical from a green run.
        """
        protected = self.managed_but_gone(".local/bin/claude-caffeinate",
                                          "tools/caffeinate/claude-caffeinate")
        retired = self.dangling_into_repo(".local/bin/claude-repo-refresh")
        proc = self.run_install()
        # The source is deliberately gone from this checkout: a required failure.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        # The block is printed last, so it also proves the run reached the end.
        self.assertIn("Required components failed:", proc.stdout)
        self.assertTrue(self.exists(protected),
                        "managed link was pruned")
        self.assertFalse(self.exists(retired),
                         "the prune no longer removes a leftover whose name the "
                         "installer does not mention anywhere — this is the "
                         "retired-tool defect, back")

    def test_the_exclusion_matches_whole_names_not_substrings(self):
        """A leftover whose path is a PREFIX of a managed one is still pruned.

        The direction matters and the first version of this test had it
        backwards. `is_managed_link` asks whether the entry appears in the
        recorded list, so an unanchored pattern wrongly protects an entry that
        is a *substring of* a managed path — not one that contains it.
        `~/.local/bin/claude-caffeinate-extra` is therefore the wrong fixture
        (nothing in the list contains it, so it is pruned either way, and the
        mutation SURVIVED). `~/.local/bin/claude-session` is the right one:
        `claude-session-claim` and `claude-session-release` are managed, so it
        sits inside their paths, and dropping the anchors protects it.

        The mirror case — an entry a managed path is a suffix of — is not
        tested, because every recorded name is an absolute path under the same
        few directories and no such pair is constructible. Stated rather than
        covered by a fixture that could not fail.
        """
        self.managed_but_gone(".local/bin/claude-session-claim",
                             "tools/session-claim/claude-session-claim")
        lookalike = self.dangling_into_repo(".local/bin/claude-session")
        proc = self.run_install()
        # The source is deliberately gone from this checkout: a required failure.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        # The block is printed last, so it also proves the run reached the end.
        self.assertIn("Required components failed:", proc.stdout)
        self.assertFalse(self.exists(lookalike),
                         "a leftover whose path sits inside a managed one was "
                         "protected — the exclusion is matching substrings "
                         "rather than whole recorded lines")


class TestThePruneRefusesWithoutAKnownRepo(InstallSandbox):
    """An empty `$REPO` turns the ownership test into `/*`, which matches every
    absolute path — so the loop would delete symlinks it does not own.

    **No current path reaches it, and the honest reason is stronger than the one
    this guard was added for.** Blanking REPO where it is computed does not even
    get here: `set -euo pipefail` aborts on the failed `cd`, and if that is
    removed the run dies at first-time setup instead (`can't open file
    '/lib/setup_config.py'`) — measured, both. So the prune is protected today by
    two unrelated upstream failures, neither of which is about ownership and
    neither of which a person editing this loop would think to preserve.

    That is what the guard is for, and it is why this test injects the state
    **immediately before the loop** rather than at the top of the script: the
    subject is the branch's behaviour, not its reachability. Testing it through
    a path that cannot currently happen would be testing the upstream failures.
    """

    def force_empty_repo(self):
        """Blank REPO in the sandbox copy, right where the prune reads it.

        Anchored on the guard's own condition so that deleting the guard breaks
        this fixture loudly instead of leaving it silently injecting nothing.
        """
        path = os.path.join(self.repo, "install.sh")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        marker = 'if [[ -z "${REPO:-}" ]]; then\n'
        self.assertIn(marker, source,
                      "the prune's empty-REPO guard is gone, or its condition "
                      "changed — this test can no longer reach the branch it "
                      "names, so fix one or the other")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source.replace(marker, 'REPO=""\n' + marker, 1))

    def test_it_refuses_and_deletes_nothing(self):
        link = self.dangling_into_repo(".local/bin/claude-retired")
        foreign = self.link(".local/bin/their-tool",
                            os.path.join(self.tmp, "elsewhere", "their-tool"))
        self.force_empty_repo()

        proc = self.run_install()

        self.assertNotEqual(proc.returncode, 0,
                            "install.sh completed with an empty REPO instead of "
                            "refusing:\n" + proc.stdout)
        self.assertIn("REPO is empty", proc.stdout + proc.stderr,
                      "it failed, but not for this reason — the message is what "
                      "tells the next person the ownership test was the risk")
        # The point of refusing rather than skipping: nothing may be removed.
        self.assertTrue(self.exists(link), "a link was deleted with no known repo")
        self.assertTrue(self.exists(foreign),
                        "a foreign link was deleted with no known repo — this is "
                        "the over-deletion the ownership test exists to prevent")

    def empty_repo_on_entry_to_the_prune_section(self):
        """Blank REPO at the END OF THE HOOKS STEP, before either loop.

        Deliberately NOT anchored on the guard, unlike `force_empty_repo`
        above, and that difference is the entire test. An injection anchored
        on the guard's own condition MOVES WITH THE GUARD, so it cannot
        observe where the guard sits: relocating the guard back below the
        `~/bin` loop would survive every other test. This anchor is fixed to
        the hooks step, which neither loop may precede, so the guard's
        position becomes observable.
        """
        path = os.path.join(self.repo, "install.sh")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        # The hooks heredoc's last line plus its terminator. Belongs to the
        # hooks step, not to either prune, so it stays put when they move.
        marker = '  exit "$UNPROTECTED_RC"\nfi\n'
        self.assertIn(marker, source,
                      "the hooks step's tail changed, so this fixture no "
                      "longer marks the start of the prune section — re-anchor "
                      "it rather than deleting the test")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source.replace(marker, marker + 'REPO=""\n', 1))

    def test_the_refusal_covers_the_bin_loop_too_not_just_the_prune(self):
        """The guard sits before BOTH loops, and this is what pins it there.

        `~/bin` fixtures specifically: the test above uses only `.local/bin`,
        so every one of its fixtures sits outside the loop this covers, and a
        guard that protected only the second loop would pass it unchanged.
        With the guard early, install refuses and `~/bin` is untouched; with
        it back at its earlier position (after the first loop) the `~/bin` walk runs first
        against an empty $REPO — where the ownership test degrades to `/*` —
        deletes a foreign link, and only then refuses.
        """
        foreign = self.link("bin/their-tool",
                            os.path.join(self.tmp, "elsewhere", "their-tool"))
        retired = self.dangling_into_repo("bin/claude-retired")
        self.empty_repo_on_entry_to_the_prune_section()

        proc = self.run_install()

        self.assertNotEqual(proc.returncode, 0,
                            "install.sh completed with an empty REPO instead of "
                            "refusing:\n" + proc.stdout)
        self.assertIn("REPO is empty", proc.stdout + proc.stderr,
                      "it failed, but not for this reason")
        self.assertTrue(self.exists(foreign),
                        "the ~/bin loop deleted a foreign link before the guard "
                        "ran — the guard is back below it, or gone")
        self.assertTrue(self.exists(retired),
                        "the ~/bin loop ran at all with no known repo; refusing "
                        "means nothing is removed, not that some things are")


class TestTheInstallerIsNeverHandedOurStdin(InstallSandbox):
    """The suite must pass from a real terminal, not only from a pipe.

    This is the defect that made the rest of this file look green while being
    unrunnable by hand: `capture_output=True` redirects stdout and stderr and
    leaves **stdin inherited**, so from a terminal the sandbox's first-time
    setup prompt blocked forever with its output swallowed by the pipe.

    The test gives its OWN stdin a pty and then runs the installer normally. If
    `run_install` stops passing `stdin=DEVNULL`, the child inherits that pty and
    the `timeout` turns the hang into a failure instead of a wedged suite.
    """

    def test_install_completes_when_the_test_itself_has_a_terminal(self):
        import pty

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        saved = os.dup(0)
        try:
            os.dup2(slave, 0)
            os.close(slave)
            self.assertTrue(sys.stdin.isatty(),
                            "the fixture failed to give this test a TTY, so it "
                            "proves nothing about the inherited-stdin case")
            try:
                proc = self.run_install("--check")
            except subprocess.TimeoutExpired:
                self.fail("install.sh blocked on stdin — run_install is not "
                          "passing stdin=DEVNULL, so the suite hangs for anyone "
                          "running it from a terminal")
        finally:
            os.dup2(saved, 0)
            os.close(saved)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Check complete", proc.stdout,
                      "install.sh did not reach the end of its run")


class TestSuiteRunnerFoundNothingInTheSandbox(InstallSandbox):
    """Guards the sandbox itself.

    If `tests/` ever stops being stripped, this file runs the entire toolkit
    suite inside one of its own test cases — slow, recursive, and it would make
    every assertion above unreliable rather than failing outright.
    """

    def test_the_sandbox_copy_has_no_test_directories(self):
        found = []
        for root, dirs, _ in os.walk(self.repo):
            if os.path.basename(root) == "tests":
                found.append(root)
        self.assertEqual(found, [],
                         "the sandbox copy still contains tests/ — this test "
                         "would recurse into the whole suite")


if __name__ == "__main__":
    unittest.main(verbosity=1)
