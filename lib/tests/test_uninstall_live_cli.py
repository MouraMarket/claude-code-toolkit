#!/usr/bin/env python3
"""Live proof: install.sh then `install.sh --uninstall --yes` against the REAL
`claude` CLI, inside a sandbox HOME.

Skipped unless TOOLKIT_LIVE_CLI=1 and a real `claude` is on PATH. The sandbox
strips every CLAUDE*, ANTHROPIC*, GIT_* and XDG_CONFIG_HOME variable, points
HOME and CLAUDE_CONFIG_DIR inside a temp dir, and the test hashes the real
~/.claude/settings.json and plugins/installed_plugins.json before and after to
prove nothing leaked out.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.realpath(os.path.join(HERE, "..", ".."))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
PLUGINS = ("toolkit-core@claude-code-toolkit", "toolkit-continuity@claude-code-toolkit")
TIMEOUT = 600

# Resolved before any HOME override, so these are the person's real files.
REAL_HOME = os.path.expanduser("~")
REAL_FILES = (
    os.path.join(REAL_HOME, ".claude", "settings.json"),
    os.path.join(REAL_HOME, ".claude", "plugins", "installed_plugins.json"),
)


def file_hash(path):
    if not os.path.isfile(path):
        return "absent"
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def entries(payload):
    """Flatten a `claude ... list --json` payload into a list of dicts."""
    if isinstance(payload, dict):
        for key in ("plugins", "marketplaces", "installed", "items"):
            if isinstance(payload.get(key), list):
                return [e for e in payload[key] if isinstance(e, dict)]
        return [payload]
    return [e for e in payload if isinstance(e, dict)]


def plugin_id(entry):
    return entry.get("id") or entry.get("plugin") or entry.get("name")


@unittest.skipUnless(os.environ.get("TOOLKIT_LIVE_CLI") == "1",
                     "set TOOLKIT_LIVE_CLI=1 to run against the real claude CLI")
class UninstallLiveCli(unittest.TestCase):
    def setUp(self):
        if not shutil.which("claude"):
            self.skipTest("no `claude` on PATH")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.realpath(self._tmp.name)
        env = {
            key: value for key, value in os.environ.items()
            if not (key.startswith(("CLAUDE", "ANTHROPIC", "GIT_")) or key == "XDG_CONFIG_HOME")
        }
        env.update({
            "HOME": self.home,
            "CLAUDE_CONFIG_DIR": os.path.join(self.home, ".claude"),
            "GIT_CONFIG_GLOBAL": os.path.join(self.home, ".gitconfig"),
            "CLAUDE_INSTALL_SKIP_SUITES": "1",
            "ALLOW_WORKTREE_INSTALL": "1",
        })
        self.env = env
        config_dir = os.path.join(self.home, ".config", "claude-code-toolkit")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "config.json"), "w") as handle:
            json.dump({"profiles": ["continuity"]}, handle)

    def run_cmd(self, args):
        result = subprocess.run(args, env=self.env, cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=TIMEOUT)
        result.detail = "cmd=%s exit=%s\nSTDOUT:\n%s\nSTDERR:\n%s" % (
            args, result.returncode, result.stdout, result.stderr)
        return result

    def list_json(self, *args):
        result = self.run_cmd(["claude", *args, "--json"])
        self.assertEqual(result.returncode, 0, result.detail)
        try:
            return entries(json.loads(result.stdout))
        except ValueError:
            self.fail("not JSON\n" + result.detail)

    def test_install_then_uninstall_round_trip(self):
        before = [file_hash(path) for path in REAL_FILES]

        installed = self.run_cmd(["bash", INSTALL_SH])
        self.assertEqual(installed.returncode, 0, installed.detail)
        listed = {plugin_id(e): e for e in self.list_json("plugin", "list")}
        for plugin in PLUGINS:
            self.assertIn(plugin, listed, "%s not listed: %s\n%s" % (
                plugin, sorted(map(str, listed)), installed.detail))
            self.assertEqual(listed[plugin].get("scope"), "user", str(listed[plugin]))

        removed = self.run_cmd(["bash", INSTALL_SH, "--uninstall", "--yes"])
        self.assertEqual(removed.returncode, 0, removed.detail)
        still = {plugin_id(e) for e in self.list_json("plugin", "list")}
        for plugin in PLUGINS:
            self.assertNotIn(plugin, still, "%s survived uninstall\n%s" % (plugin, removed.detail))
        markets = {e.get("name") for e in self.list_json("plugin", "marketplace", "list")}
        self.assertNotIn("claude-code-toolkit", markets, removed.detail)

        self.assertEqual(before, [file_hash(path) for path in REAL_FILES],
                         "the real ~/.claude files changed")


if __name__ == "__main__":
    unittest.main()
