#!/usr/bin/env python3
"""Pins the toolkit plugin packages: manifests, hook parity with the frozen
legacy template, the legacy-command list, and the `hook_manifest` parser."""

import collections
import copy
import glob
import json
import os
import re
import shlex
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

FIXTURE = os.path.join(REPO_ROOT, "lib", "tests", "fixtures", "settings.hooks.legacy.json")
MARKETPLACE_JSON = os.path.join(REPO_ROOT, ".claude-plugin", "marketplace.json")
PLUGIN_JSON = os.path.join(REPO_ROOT, "plugin", ".claude-plugin", "plugin.json")
HOST_JSON = os.path.join(REPO_ROOT, "claude", "settings.host.json")
LEGACY_JSON = os.path.join(REPO_ROOT, "claude", "legacy-hook-commands.json")
OLD_PREFIX = "__HOME__/.local/bin/"

# A literal home directory inside a command: macOS/Linux or Windows separators.
_HARDCODED_HOME = re.compile(r'[\\/](?:Users|home)[\\/][^\s"\'\\/]+')


# The old template also shipped these `__HOME__/bin/<name>` spellings.
BIN_SPELLINGS = {
    "__HOME__/bin/" + name
    for name in (
        "claude-danger-guard",
        "claude-repo-freshness",
        "claude-thread-deliver",
        "claude-thread-watch",
        "claude-title-session",
        "claude-worktree-guard",
    )
}

# The older personal template's hooks that the shared one never had. An old
# personal install carries them in settings.json, so install.sh strips them too.
PERSONAL_ONLY = {"__HOME__/.local/bin/claude-prompt-optimizer"}

# Handlers the plugin ships that the frozen legacy template never had.
PLUGIN_ONLY = [
    ("PreToolUse", "Bash|PowerShell|Monitor", "claude-hook-heartbeat", [], 5),
    ("SessionStart", None, "claude-hook-heartbeat", ["--session-start"], 5),
    ("PreToolUse", "Agent", "claude-prompt-optimizer", [], 5),
    ("PostToolUse", "Bash|Read|Grep|Glob|mcp__.*", "claude-tool-rules", [], 5),
    ("SessionStart", None, "claude-tool-rules", ["--reset"], 5),
]

# Each non-core plugin dir also registers the heartbeat's SessionStart handler, as the
# last hook of its SessionStart block, so a session can be proven per profile.
HEARTBEAT_START = ("SessionStart", None, "claude-hook-heartbeat", ["--session-start"], 5)
HEARTBEAT_DIRS = ("plugin-build", "plugin-continuity", "plugin-mac", "plugin-maintainer")

# Handlers the frozen legacy template has that the plugin has since retired.
# The template stays frozen: install.sh still strips the legacy command.
PLUGIN_RETIRED = [
    ("SessionStart", None, "claude-build-route", [], 5),
]

# Handlers whose timeout the plugin has deliberately changed since the template
# froze: (event, exe) -> (frozen timeout, plugin timeout). The template stays
# frozen; the old value is asserted so a stale entry here fails loudly.
# repo-freshness 25 -> 30: parallel pulls widened NET_BUDGET, and its
# TestWholeHookTimeoutBudget needs a 30s declared timeout.
PLUGIN_RETIMED = {
    ("SessionStart", "claude-repo-freshness"): (25, 30),
}


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _fixture_reduced():
    """(event, matcher, exe, args, timeout) for each handler of the frozen fixture."""
    rows = []
    for event, blocks in _load_json(FIXTURE)["hooks"].items():
        for block in blocks:
            for hook in block["hooks"]:
                command = hook["command"]
                assert command.startswith(OLD_PREFIX), command
                tokens = shlex.split(command[len(OLD_PREFIX):])
                timeout = hook.get("timeout")
                retimed = PLUGIN_RETIMED.get((event, tokens[0]))
                if retimed is not None:
                    assert timeout == retimed[0], (event, tokens[0], timeout)
                    timeout = retimed[1]
                rows.append((event, block.get("matcher"), tokens[0], tokens[1:], timeout))
    return rows


def _multiset(rows):
    """Counter of reduced rows; args become a tuple so the row is hashable."""
    return collections.Counter((e, m, x, tuple(a), t) for e, m, x, a, t in rows)


def _manifest_reduced(hook_manifest):
    manifest = hook_manifest.load(REPO_ROOT)
    return [
        (r["event"], r["matcher"], r["exe"], r["args"], r["timeout"])
        for r in hook_manifest.registrations(manifest)
    ]


class TestParity(unittest.TestCase):
    def test_plugin_hooks_match_frozen_template(self):
        import hook_manifest

        reduced = _manifest_reduced(hook_manifest)
        for row in PLUGIN_ONLY:
            self.assertEqual(reduced.count(row), 1 + (
                len(HEARTBEAT_DIRS) if row == HEARTBEAT_START else 0), row)
            reduced.remove(row)
        for _ in HEARTBEAT_DIRS:
            reduced.remove(HEARTBEAT_START)
        expected = _fixture_reduced()
        for row in PLUGIN_RETIRED:
            self.assertEqual(expected.count(row), 1, row)
            expected.remove(row)
        # Multisets: coverage parity, not order. Order across plugins is not
        # meaningful; per-plugin order is pinned by TestProfilePlacement.
        self.assertEqual(_multiset(reduced), _multiset(expected))

    def test_events_match(self):
        """The union covers the same events as the legacy template. Order across
        plugins is not meaningful (Claude Code runs a hook event's handlers in
        parallel); per-plugin order is pinned by
        TestProfilePlacement.test_each_profile_keeps_the_legacy_order."""
        import hook_manifest

        plugin_events = set(hook_manifest.load(REPO_ROOT).keys())
        self.assertEqual(plugin_events, set(_load_json(FIXTURE)["hooks"].keys()))


GUARD_MATCHER = "Bash|PowerShell|Monitor"


def _exe_and_args(hook_manifest, handler):
    relpath, args = hook_manifest.parse_command(handler["command"])
    return os.path.basename(relpath), args


def _assert_heartbeat_placement(hook_manifest, hooks):
    """Asserts on the parsed block structure; reduced rows discard block identity."""
    blocks = [b for b in hooks["PreToolUse"] if b.get("matcher") == GUARD_MATCHER]
    assert len(blocks) == 1, "expected exactly one %s PreToolUse block, found %d" % (
        GUARD_MATCHER, len(blocks))
    parsed = [_exe_and_args(hook_manifest, h) for h in blocks[0]["hooks"]]
    assert parsed == [("claude-danger-guard", []), ("claude-hook-heartbeat", [])], parsed

    groups = [b for b in hooks["SessionStart"] if "matcher" not in b]
    assert len(groups) == 1, "expected one matcher-less SessionStart group"
    last = _exe_and_args(hook_manifest, groups[0]["hooks"][-1])
    assert last == ("claude-hook-heartbeat", ["--session-start"]), last


class TestHeartbeatPlacement(unittest.TestCase):
    def test_heartbeat_sits_in_the_guard_block_and_last_in_session_start(self):
        import hook_manifest

        _assert_heartbeat_placement(
            hook_manifest, hook_manifest.load_plugin(REPO_ROOT, "core"))

    def test_placement_check_rejects_a_separate_block_with_the_same_matcher(self):
        import hook_manifest

        hooks = copy.deepcopy(hook_manifest.load_plugin(REPO_ROOT, "core"))
        guard_block = next(b for b in hooks["PreToolUse"] if b.get("matcher") == GUARD_MATCHER)
        heartbeat = guard_block["hooks"].pop()
        hooks["PreToolUse"].append({"matcher": GUARD_MATCHER, "hooks": [heartbeat]})
        with self.assertRaises(AssertionError):
            _assert_heartbeat_placement(hook_manifest, hooks)


# Spelled out by hand from the plan's placement table, never derived from a manifest.
PLACEMENT = {
    "claude-danger-guard": "core",
    "claude-hook-heartbeat": "core",
    "claude-worktree-guard": "core",
    "claude-distill-write-guard": "core",
    "claude-toolkit-integrity": "core",
    "claude-tool-rules": "continuity",
    "claude-tooling-digest": "core",
    "claude-repo-freshness": "core",
    "claude-thread-watch": "continuity",
    "claude-thread-deliver": "continuity",
    "claude-title-session": "continuity",
    "claude-usage-watch": "continuity",
    "claude-usage-dispatch": "continuity",
    "claude-usage-subagent": "continuity",
    "claude-usage-postmortem": "continuity",
    "claude-memory-lint": "continuity",
    "claude-memory-routing": "continuity",
    "claude-context-budget": "continuity",
    "claude-context-growth": "continuity",
    "claude-model-guard": "builder",
    "claude-prompt-optimizer": "builder",
    "claude-session-claim": "builder",
    "claude-session-release": "builder",
    "claude-build-cost-record": "builder",
    "claude-base-pin": "builder",
    "claude-spec-archive": "builder",
    "claude-harness-check": "maintainer",
    "claude-caffeinate": "mac",
}
PROFILES = ("core", "continuity", "builder", "maintainer", "mac")


class TestProfilePlacement(unittest.TestCase):
    def _profile_rows(self, hook_manifest, profile):
        rows = [
            (r["event"], r["matcher"], r["exe"], r["args"], r["timeout"])
            for r in hook_manifest.registrations(hook_manifest.load_plugin(REPO_ROOT, profile))
        ]
        for row in PLUGIN_ONLY:
            if row in rows:
                rows.remove(row)
        return rows

    def test_each_profile_keeps_the_legacy_order(self):
        import hook_manifest

        legacy = _fixture_reduced()
        for row in PLUGIN_RETIRED:
            legacy.remove(row)
        for profile in PROFILES:
            with self.subTest(profile=profile):
                expected = [row for row in legacy if PLACEMENT[row[2]] == profile]
                self.assertEqual(self._profile_rows(hook_manifest, profile), expected)

    def test_every_registration_sits_in_its_profile(self):
        import hook_manifest

        for row in hook_manifest.plugin_registrations(REPO_ROOT):
            if row["exe"] == "claude-hook-heartbeat" and row["args"] == ["--session-start"]:
                continue  # registered in every plugin; pinned by TestProfileHeartbeat
            with self.subTest(exe=row["exe"]):
                self.assertEqual(PLACEMENT[row["exe"]], row["profile"])

    def test_placement_names_every_registered_hook(self):
        import hook_manifest

        registered = {r["exe"] for r in hook_manifest.plugin_registrations(REPO_ROOT)}
        self.assertEqual(set(PLACEMENT), registered)

    def test_one_block_per_matcher_per_event(self):
        import hook_manifest

        for profile in PROFILES:
            for event, blocks in hook_manifest.load_plugin(REPO_ROOT, profile).items():
                matchers = [b.get("matcher") for b in blocks]
                with self.subTest(profile=profile, event=event):
                    self.assertEqual(len(matchers), len(set(matchers)), matchers)


class TestProfileHeartbeat(unittest.TestCase):
    def test_every_non_core_plugin_ends_session_start_with_the_heartbeat(self):
        import hook_manifest

        for directory in HEARTBEAT_DIRS:
            with self.subTest(plugin=directory):
                hooks = _load_json(os.path.join(REPO_ROOT, directory, "hooks", "hooks.json"))
                blocks = [b for b in hooks["hooks"]["SessionStart"] if "matcher" not in b]
                self.assertEqual(len(blocks), 1)
                last = blocks[0]["hooks"][-1]
                self.assertEqual(_exe_and_args(hook_manifest, last),
                                 ("claude-hook-heartbeat", ["--session-start"]))
                self.assertEqual(last["timeout"], 5)
                self.assertEqual(last["type"], "command")
                beats = [h for b in hooks["hooks"]["SessionStart"] for h in b["hooks"]
                         if "claude-hook-heartbeat" in h["command"]]
                self.assertEqual(len(beats), 1)

    def test_heartbeat_dirs_are_every_plugin_dir_but_core(self):
        dirs = sorted(os.path.basename(os.path.dirname(os.path.dirname(p))) for p in glob.glob(
            os.path.join(REPO_ROOT, "plugin-*", "hooks", "hooks.json")))
        self.assertEqual(dirs, sorted(HEARTBEAT_DIRS))


class TestCommandsResolve(unittest.TestCase):
    def test_every_command_parses_and_resolves_into_tools(self):
        import hook_manifest

        regs = hook_manifest.registrations(hook_manifest.load(REPO_ROOT))
        self.assertTrue(regs)
        tools_root = os.path.realpath(os.path.join(REPO_ROOT, "tools"))
        checked = 0
        for reg in regs:
            relpath, _args = hook_manifest.parse_command(reg["command"])
            target = hook_manifest.resolve(REPO_ROOT, relpath)
            checked += 1
            self.assertTrue(os.path.exists(target), target)
            self.assertTrue(os.access(target, os.X_OK), target)
            self.assertTrue(os.path.realpath(target).startswith(tools_root + os.sep), target)
        self.assertGreater(checked, 0, "no hook targets: nothing was checked")


class TestSymlinks(unittest.TestCase):
    def test_relative_symlinks(self):
        self.assertEqual(os.readlink(os.path.join(REPO_ROOT, "plugin", "tools")), "../tools")
        self.assertEqual(os.readlink(os.path.join(REPO_ROOT, "plugin", "lib")), "../lib")


class TestManifests(unittest.TestCase):
    def test_marketplace(self):
        market = _load_json(MARKETPLACE_JSON)
        self.assertEqual(market["name"], "claude-code-toolkit")
        self.assertEqual([p["name"] for p in market["plugins"]],
                         ["toolkit-core", "toolkit-continuity", "toolkit-build",
                          "toolkit-maintainer", "toolkit-mac", "skill-prompt-optimizer",
                          "skill-humanizer", "skill-claude-teacher",
                          "skill-chrome-handoff", "skill-port-github-repo",
                          "skill-model-routing"])
        self.assertEqual(market["plugins"][0]["source"], "./plugin")

    def test_no_plugin_sets_a_version(self):
        # A version would pin GitHub installs to one cached copy until someone
        # bumps it; without one they follow the commit (scripts/check_layout.py
        # refuses one too).
        market = _load_json(MARKETPLACE_JSON)
        self.assertEqual(len(market["plugins"]), 11)
        for entry in market["plugins"]:
            self.assertNotIn("version", entry, entry["name"])
            manifest = _load_json(os.path.join(REPO_ROOT, entry["source"], ".claude-plugin",
                                               "plugin.json"))
            self.assertNotIn("version", manifest, entry["name"])
        self.assertEqual(_load_json(PLUGIN_JSON)["name"], "toolkit-core")


class TestLegacyCommands(unittest.TestCase):
    def test_legacy_list(self):
        commands = _load_json(LEGACY_JSON)["commands"]
        self.assertEqual(len(commands), 35)
        self.assertEqual(len(set(commands)), len(commands))
        self.assertEqual(commands, sorted(commands))
        fixture_commands = {
            hook["command"]
            for blocks in _load_json(FIXTURE)["hooks"].values()
            for block in blocks
            for hook in block["hooks"]
        }
        self.assertTrue(fixture_commands <= set(commands))
        self.assertEqual(set(commands) - fixture_commands, BIN_SPELLINGS | PERSONAL_ONLY)

    def test_legacy_commands_substitutes_home(self):
        import hook_manifest

        result = hook_manifest.legacy_commands(REPO_ROOT, "/h")
        self.assertEqual(len(result), 35)
        self.assertIn("/h/bin/claude-danger-guard", result)
        self.assertFalse(any("__HOME__" in c for c in result))


class TestHostSettings(unittest.TestCase):
    def test_status_line_matches_frozen_template(self):
        self.assertEqual(_load_json(HOST_JSON)["statusLine"], _load_json(FIXTURE)["statusLine"])


class TestParseCommand(unittest.TestCase):
    def test_rejects_bad_commands(self):
        import hook_manifest

        for bad in ("__HOME__/.local/bin/x", "${CLAUDE_PLUGIN_ROOT}/x", "",
                    '"${CLAUDE_PLUGIN_ROOT}"/', '"${CLAUDE_PLUGIN_ROOT}"/   ',
                    "/usr/local/bin/claude-some-long-hook-command --session-start"):
            with self.assertRaises(hook_manifest.ManifestError, msg=bad):
                hook_manifest.parse_command(bad)

    def test_parses_args(self):
        import hook_manifest

        relpath, args = hook_manifest.parse_command('"${CLAUDE_PLUGIN_ROOT}"/tools/a/b --hook x')
        self.assertEqual((relpath, args), ("tools/a/b", ["--hook", "x"]))


class TestLoadValidatesNestedShape(unittest.TestCase):
    """load() raises ManifestError for a malformed manifest, never
    AttributeError/TypeError out of registrations() later."""

    BAD = {
        "event value not a list": {"PreToolUse": {}},
        "block is null": {"PreToolUse": [None]},
        "block is a string": {"PreToolUse": ["x"]},
        "matcher not a str": {"PreToolUse": [{"matcher": 5, "hooks": []}]},
        "hooks not a list": {"PreToolUse": [{"hooks": {}}]},
        "hooks is null": {"PreToolUse": [{"hooks": None}]},
        "handler not a dict": {"PreToolUse": [{"hooks": ["x"]}]},
        "command not a str": {"PreToolUse": [{"hooks": [{"command": 5}]}]},
        "command absent": {"PreToolUse": [{"hooks": [{"type": "command"}]}]},
        "command lacks the plugin-root prefix": {"PreToolUse": [{"hooks": [
            {"command": "not-plugin-root", "timeout": 5}]}]},
        "timeout not an int": {"PreToolUse": [{"hooks": [
            {"command": '"${CLAUDE_PLUGIN_ROOT}"/x', "timeout": "5"}]}]},
        "timeout is a bool": {"PreToolUse": [{"hooks": [
            {"command": '"${CLAUDE_PLUGIN_ROOT}"/x', "timeout": True}]}]},
    }

    def _load(self, hooks):
        import tempfile
        import shutil
        import hook_manifest

        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "plugin", "hooks"))
        with open(os.path.join(root, hook_manifest.PLUGIN_HOOKS), "w") as handle:
            json.dump({"hooks": hooks}, handle)
        return hook_manifest.load(root)

    def test_every_malformed_shape_raises_manifest_error(self):
        import hook_manifest

        for label, hooks in self.BAD.items():
            with self.subTest(shape=label):
                with self.assertRaises(hook_manifest.ManifestError):
                    self._load(hooks)

    def test_a_well_formed_manifest_still_loads(self):
        good = {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": '"${CLAUDE_PLUGIN_ROOT}"/x', "timeout": 5}]},
            {"hooks": []}]}
        self.assertEqual(good, self._load(good))


class TestWantedStatusLine(unittest.TestCase):
    """install.sh writes the statusLine itself, so its quoting is the
    toolkit's: a HOME holding a shell-special character is quoted, and any
    other HOME (an ʻokina included) is substituted exactly as before."""

    def _repo(self, host):
        import shutil
        import tempfile

        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "claude"))
        with open(os.path.join(root, "claude", "settings.host.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(host, handle)
        return root

    def _wanted(self, home, host=None):
        import hook_manifest

        if host is None:
            host = {"statusLine": {"type": "command",
                                   "command": "__HOME__/.local/bin/claude-x"}}
        return hook_manifest.wanted_status_line(self._repo(host), home)

    def test_a_plain_home_is_substituted_unquoted(self):
        self.assertEqual(self._wanted("/Users/a"),
                         {"type": "command", "command": "/Users/a/.local/bin/claude-x"})

    def test_an_okina_home_stays_unquoted(self):
        self.assertEqual(self._wanted("/Users/Kaua\u02bbi")["command"],
                         "/Users/Kaua\u02bbi/.local/bin/claude-x")

    def test_a_spaced_home_is_quoted(self):
        self.assertEqual(self._wanted("/Users/my home")["command"],
                         "'/Users/my home'/.local/bin/claude-x")

    def test_no_usable_status_line_is_none(self):
        for host in ({}, {"statusLine": "x"}, {"statusLine": {"command": 5}}, []):
            with self.subTest(host=host):
                self.assertIsNone(self._wanted("/Users/a", host))


class TestTimeouts(unittest.TestCase):
    def test_every_handler_declares_positive_int_timeout(self):
        import hook_manifest

        for reg in hook_manifest.registrations(hook_manifest.load(REPO_ROOT)):
            timeout = reg["timeout"]
            self.assertIsInstance(timeout, int, reg["command"])
            self.assertNotIsInstance(timeout, bool, reg["command"])
            self.assertGreater(timeout, 0, reg["command"])


def _walk_commands(node):
    """Yield every `"command"` string in a parsed JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                yield value
            else:
                yield from _walk_commands(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_commands(item)


def _hook_commands():
    """Every hook command of every plugin*/hooks/hooks.json, as (file, command).

    A raw JSON walk, not `hook_manifest`, so a plugin directory nothing
    registers, or a malformed command, is still scanned."""
    pattern = os.path.join(REPO_ROOT, "plugin*", "hooks", "hooks.json")
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as handle:
            for command in _walk_commands(json.load(handle)):
                yield os.path.relpath(path, REPO_ROOT), command


def _status_line_command():
    with open(HOST_JSON, encoding="utf-8") as handle:
        return json.load(handle)["statusLine"]["command"]


class TestNoCommandHardcodesAHomePath(unittest.TestCase):
    def test_no_command_hardcodes_a_home_path(self):
        scanned = list(_hook_commands())
        scanned.append(("claude/settings.host.json", _status_line_command()))
        offenders = [(source, command) for source, command in scanned
                     if _HARDCODED_HOME.search(command)]
        self.assertEqual(
            offenders, [],
            "a command names a literal home directory. It ships unrewritten, so "
            "the hook is inert on every machine but the author's, and nothing "
            "reports it. Use \"${CLAUDE_PLUGIN_ROOT}\"/ (or __HOME__ for the "
            "host settings) instead.")

    def test_the_scan_reads_real_commands(self):
        commands = [command for _, command in _hook_commands()]
        self.assertGreaterEqual(len(commands), 10, "the scan found almost no hook commands")
        self.assertTrue(
            any(c.startswith('"${CLAUDE_PLUGIN_ROOT}"/') for c in commands),
            "no hook command starts with the plugin-root variable")
        self.assertTrue(_status_line_command().startswith("__HOME__/"),
                        "the statusLine command no longer uses the __HOME__ placeholder")

    def test_the_pattern_catches_a_home_path(self):
        for bad in ("test -x /Users/someone/bin/mr",
                    "/home/someone/.local/bin/x",
                    "C:\\Users\\someone\\bin\\x"):
            self.assertTrue(_HARDCODED_HOME.search(bad), bad)
        for good in ('"${CLAUDE_PLUGIN_ROOT}"/tools/x', "__HOME__/.local/bin/x"):
            self.assertIsNone(_HARDCODED_HOME.search(good), good)


if __name__ == "__main__":
    unittest.main()
