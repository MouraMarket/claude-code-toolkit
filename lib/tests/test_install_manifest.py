#!/usr/bin/env python3
"""Tests for `install.sh --manifest`.

**What the flag is for.** `tools/setup-audit` needs the destinations
`install.sh` manages without re-deriving them from a second, hand-maintained
list, the hazard `EXECUTABLE_NAMES` already demonstrates. `--manifest` reuses
the real `link` and `note_managed_link` calls in report mode, tab-separated,
and stops before touching anything.

**What these tests pin.**

1. The row counts track the real calls: one `link\t` row per `^link\s+"` call
   site, and no `managed\t` rows at all (no top-level `note_managed_link` call
   is left once the plugins ship the skills). Never a count typed here, so a
   new `link` call cannot quietly drift from what this asserts.
2. Every `link` row names a real repo file and a real sandbox destination.
3. The flag changes nothing: the sandbox `HOME` (empty going in) is untouched.
4. `--manifest` takes no other arguments.
5. `--links-only` alone, the flag `--manifest` implies, never leaks a
   manifest row; the rows exist only under `--manifest` itself.

`HOME` is redirected to a fresh, empty sandbox for every test, so nothing
here touches the real machine.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

import install_fixture as fx

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
INSTALL_SH = os.path.join(REPO, "install.sh")

# Captured at import, before any test ever overrides HOME.
REAL_HOME = os.path.expanduser("~")

# The guards' escape-hatch variables. An ambient one must never reach a run.
GUARD_ESCAPE_HATCHES = ("DANGER_ALLOW", "WORKTREE_ALLOW", "WT_ALLOW_MAIN")


def _bash_available():
    return shutil.which("bash") is not None


def sandbox_home():
    return tempfile.mkdtemp(prefix="install-manifest-test-")


def env_for(home, extra=None):
    """A hermetic subprocess environment: start from a copy of `os.environ`,
    strip every `GIT_*` var plus `XDG_CONFIG_HOME`, `CLAUDE_CONFIG_DIR` and the
    guard escape hatches, then point `HOME` and `CLAUDE_TOOLKIT_CONFIG` into
    the sandbox. `extra` is applied last, never inherited ambiently."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_") or key in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR") \
           or key in GUARD_ESCAPE_HATCHES:
            del env[key]
    env["HOME"] = home
    env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(
        home, ".config", "claude-code-toolkit", "config.json")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if extra:
        env.update(extra)
    assert env["HOME"] != REAL_HOME
    env = fx.shim_env_for_home(env, home)
    return env


def snapshot_tree(root):
    """Every path under `root`, relative and sorted. The sandbox starts empty,
    so this proves it is still empty after a run. System Python writes its
    bytecode cache under a redirected HOME (`Library/Caches/com.apple.python`)
    whatever the installer does, so that one tree is skipped."""
    found = []
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root)
        if rel_base == "Library" or rel_base.startswith("Library" + os.sep):
            continue
        for name in dirs + files:
            rel = os.path.relpath(os.path.join(base, name), root)
            if rel == "Library":
                continue
            found.append(rel)
    return sorted(found)


def run_install(home, *args):
    return subprocess.run(
        ["bash", INSTALL_SH] + list(args),
        capture_output=True, text=True, env=env_for(home), cwd=REPO,
        stdin=subprocess.DEVNULL, timeout=60)


def link_call_count():
    """Positive control lives beside its use: the count that must be > 0."""
    with open(INSTALL_SH, encoding="utf-8") as handle:
        return sum(1 for line in handle if re.match(r'^link\s+"', line))


def managed_call_count():
    with open(INSTALL_SH, encoding="utf-8") as handle:
        return sum(1 for line in handle
                   if re.match(r'^note_managed_link\s+"', line))


class ManifestSandbox(unittest.TestCase):
    """A throwaway, empty HOME. Nothing here touches the real machine."""

    def setUp(self):
        if not _bash_available():
            self.skipTest("bash not available")
        if not os.path.isfile(INSTALL_SH):
            self.skipTest("install.sh not found next to lib/")
        self.home = sandbox_home()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)


class TestRowCountsTrackTheRealCalls(ManifestSandbox):

    def test_exit_zero_and_row_counts_match_the_real_calls(self):
        proc = run_install(self.home, "--manifest")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        link_rows = [l for l in lines if l.startswith("link\t")]
        managed_rows = [l for l in lines if l.startswith("managed\t")]

        expected_links = link_call_count()
        self.assertGreater(expected_links, 0,
                           "positive control: no link calls found in install.sh")
        self.assertEqual(len(link_rows), expected_links, proc.stdout)
        self.assertEqual(managed_rows, [], proc.stdout)
        self.assertEqual(managed_call_count(), 0,
                         "a top-level note_managed_link call is back: the "
                         "plugins ship the skills, so nothing hand-registers a link")


class TestItStopsAfterTheLastRow(ManifestSandbox):
    """`--manifest` implies `--links-only`. Without its own exit it would fall
    into that flag's verdict, which in manifest mode has counted nothing and
    says "every link is in place": a claim about links nobody checked."""

    def test_the_last_line_printed_is_a_link_row(self):
        proc = run_install(self.home, "--manifest")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        self.assertTrue(lines, "no output at all")
        self.assertTrue(lines[-1].startswith("link\t"),
                        "--manifest printed past its last row: %r" % lines[-1])
        self.assertNotIn("every link is in place", proc.stdout)


class TestEveryLinkRowNamesRealPlaces(ManifestSandbox):

    def test_every_link_row_has_three_fields_pointing_at_real_places(self):
        proc = run_install(self.home, "--manifest")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        link_rows = [l for l in proc.stdout.splitlines()
                     if l.startswith("link\t")]
        self.assertTrue(link_rows, "no link rows to check")
        for row in link_rows:
            fields = row.split("\t")
            self.assertEqual(len(fields), 3, row)
            _, target, dest = fields
            self.assertTrue(os.path.isabs(target), row)
            self.assertTrue(
                target == REPO or target.startswith(REPO + os.sep), row)
            self.assertTrue(os.path.exists(target), row)
            self.assertTrue(os.path.isabs(dest), row)
            self.assertTrue(dest.startswith(self.home + os.sep), row)


class TestNothingChanges(ManifestSandbox):

    def test_the_sandbox_home_is_untouched(self):
        before = snapshot_tree(self.home)
        proc = run_install(self.home, "--manifest")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = snapshot_tree(self.home)
        self.assertEqual(before, after)


class TestManifestTakesNoOtherArguments(ManifestSandbox):

    def test_manifest_with_another_argument_is_rejected(self):
        proc = run_install(self.home, "--manifest", "--check")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--manifest takes no other arguments", proc.stderr)
        self.assertFalse(
            any(l.startswith("link\t") for l in proc.stdout.splitlines()),
            proc.stdout)


class TestLinksOnlyCarriesNoManifestRows(ManifestSandbox):
    """The rows appear only under `--manifest` itself: `--links-only` does not
    set `MANIFEST`, so `link()` and `note_managed_link` must fall back to their
    ordinary behaviour rather than leaking a manifest line."""

    def test_links_only_carries_no_manifest_rows(self):
        proc = run_install(self.home, "--links-only")
        lines = proc.stdout.splitlines()
        self.assertTrue(any("Executables -> ~/.local/bin" in l for l in lines),
                        "control: --links-only did not reach the link calls")
        self.assertFalse(any(l.startswith("link\t") for l in lines), proc.stdout)
        self.assertFalse(any(l.startswith("managed\t") for l in lines), proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
