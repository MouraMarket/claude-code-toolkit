#!/usr/bin/env python3
"""Behavioural tests for scripts/setup-plugins.sh.

The script drives the `claude` CLI, so these run the REAL script against the
shared shim from `install_fixture.py` and a scratch HOME, and assert on the exit
code, the output and the shim's argv log. Most cases run it against a synthetic
catalog (SETUP_PLUGINS_CATALOG) with a private team marketplace: its two
required plugins fail the run when missing; optional ones never do. The shipped
catalog has its own class at the end.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import install_fixture as fx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "setup-plugins.sh")
SHIPPED_CATALOG = os.path.join(REPO_ROOT, "claude", "recommended-plugins.json")

OFFICIAL = "claude-plugins-official"
TEAM = "example-team"
TEAM_REPO = "example-team/claude-skills"
# One name is a prefix of the other on purpose (test_8).
REQUIRED = ["skills@" + TEAM, "skills-dev@" + TEAM]
OPTIONAL = [n + "@" + OFFICIAL for n in (
    "claude-md-management", "frontend-design", "remember", "context7", "figma")]
ALL = REQUIRED + OPTIONAL
BOTH_REPOS = {"anthropics/claude-plugins-official": OFFICIAL,
              TEAM_REPO: TEAM}
NO_ACCESS = {"anthropics/claude-plugins-official": OFFICIAL}
TEAM_CATALOG = {
    "marketplaces": [
        {"name": OFFICIAL, "source": "anthropics/claude-plugins-official", "auto_update": False},
        {"name": TEAM, "source": TEAM_REPO, "auto_update": True},
    ],
    "plugins": ([{"tier": "optional", "id": spec, "why": "optional fixture plugin."}
                 for spec in OPTIONAL]
                + [{"tier": "required", "id": spec, "why": "required fixture plugin."}
                   for spec in REQUIRED]),
}


class SetupPluginsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.bin = os.path.join(self.tmp, "bin")
        self.state = os.path.join(self.tmp, "state.json")
        fx.install_claude_shim(self.bin, self.state)
        # The GitHub-login probe must never reach the network or the real
        # keychain: a fake ssh that prints TEST_SSH_GREETING, and no system git
        # config (macOS ships credential.helper=osxkeychain there).
        # It logs each call, so a test can tell the probe never ran.
        self.ssh_log = os.path.join(self.tmp, "ssh.log")
        fake_ssh = os.path.join(self.bin, "ssh")
        with open(fake_ssh, "w") as handle:
            handle.write('#!/bin/sh\necho ssh >> "%s"\n' % self.ssh_log
                         + 'printf \'%s\\n\' "${TEST_SSH_GREETING:-Permission denied (publickey).}" >&2\nexit 1\n')
        os.chmod(fake_ssh, 0o755)
        self.catalog = os.path.join(self.tmp, "catalog.json")
        with open(self.catalog, "w") as handle:
            json.dump(TEAM_CATALOG, handle)

    def run_script(self, *args, github=BOTH_REPOS, catalog="team", **extra_env):
        base = {"HOME": self.home, "PATH": os.environ.get("PATH", "")}
        env = fx.shim_env(base, self.bin, self.state)
        if catalog == "team":
            env["SETUP_PLUGINS_CATALOG"] = self.catalog
        elif catalog is not None:
            env["SETUP_PLUGINS_CATALOG"] = catalog
        env["CLAUDE_SHIM_GITHUB_MARKETPLACES"] = json.dumps(github)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.path.join(self.home, ".gitconfig")
        for leak in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_DIR"):
            env.pop(leak, None)
        env.update(extra_env)
        result = subprocess.run(["bash", SCRIPT] + list(args), env=env, cwd=self.tmp,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True)
        return result.returncode, result.stdout

    def final_state(self):
        with open(self.state) as handle:
            return json.load(handle)

    def mutating_calls(self):
        return [c for c in fx.shim_log(self.state)
                if c.startswith(("plugin install", "plugin enable", "plugin marketplace add",
                                 "plugin update", "plugin marketplace update"))]

    def seed_all(self, enabled=True, **overrides):
        plugins = [(spec, overrides.get(spec, enabled), "user") for spec in ALL]
        fx.seed_plugins(self.state, [OFFICIAL, TEAM], plugins)

    def test_1_everything_installs(self):
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("Done.", out)
        user = {p["id"] for p in self.final_state()["plugins"]
                if p["scope"] == "user" and p["enabled"]}
        self.assertEqual(user, set(ALL))

    def test_2_every_install_fails(self):
        code, out = self.run_script(CLAUDE_SHIM_FAIL="install")
        self.assertEqual(code, 1, out)
        self.assertNotIn("Done.", out)
        self.assertIn("Not done: 2 required", out)

    def test_3_private_marketplace_unreachable(self):
        code, out = self.run_script(github=NO_ACCESS)
        self.assertEqual(code, 1, out)
        for spec in REQUIRED:
            self.assertIn(spec, out)
        installed = {p["id"] for p in self.final_state()["plugins"]}
        self.assertEqual(installed, set(OPTIONAL))

    def test_4_install_that_changes_nothing(self):
        code, out = self.run_script(CLAUDE_SHIM_NOOP="install")
        self.assertEqual(code, 1, out)
        self.assertNotIn("Done.", out)

    def test_5_disabled_required_is_enabled(self):
        self.seed_all(**{REQUIRED[0]: False})
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("plugin enable skills@example-team --scope user",
                      fx.shim_log(self.state))

    def test_6_disabled_optional_is_left_alone(self):
        self.seed_all(**{"figma@" + OFFICIAL: False})
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("left as you set it", out)
        self.assertFalse([c for c in fx.shim_log(self.state) if c.startswith("plugin enable")])

    def test_7_project_scope_is_not_present(self):
        fx.seed_plugins(self.state, [OFFICIAL, TEAM], [(REQUIRED[0], True, "project")])
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("plugin install skills@example-team --scope user",
                      fx.shim_log(self.state))

    def test_8_prefix_collision(self):
        fx.seed_plugins(self.state, [OFFICIAL, TEAM], [(REQUIRED[1], True, "user")])
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("plugin install skills@example-team --scope user",
                      fx.shim_log(self.state))

    def test_9_check_changes_nothing(self):
        fx.seed_plugins(self.state, [OFFICIAL, TEAM],
                        [(s, True, "user") for s in ALL if s != REQUIRED[0]])
        code, out = self.run_script("--check")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.mutating_calls(), [])
        self.seed_all()
        code, out = self.run_script("--check")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutating_calls(), [])

    def test_10_unreadable_state(self):
        code, out = self.run_script(CLAUDE_SHIM_BAD_JSON="1")
        self.assertEqual(code, 1, out)
        self.assertNotIn("Done.", out)

    def test_11_one_optional_fails(self):
        code, out = self.run_script(CLAUDE_SHIM_FAIL="figma@" + OFFICIAL)
        self.assertEqual(code, 0, out)
        self.assertIn("1 optional plugin(s) not installed", out)

    def seed_versions(self, versions):
        fx.seed_plugins(self.state, [OFFICIAL, TEAM],
                        [(spec, True, "user", versions.get(spec, "1.0.0")) for spec in ALL])

    def test_12_stale_required_is_updated_and_reported(self):
        self.seed_versions({REQUIRED[0]: "3.0.1", REQUIRED[1]: "5.0.0"})
        latest = {REQUIRED[0]: "4.0.0", REQUIRED[1]: "6.0.0"}
        code, out = self.run_script(CLAUDE_SHIM_LATEST=json.dumps(latest))
        self.assertEqual(code, 0, out)
        log = fx.shim_log(self.state)
        self.assertIn("plugin marketplace update " + TEAM, log)
        for spec in REQUIRED:
            self.assertIn("plugin update %s --scope user" % spec, log)
        self.assertIn("skills@example-team updated 3.0.1 -> 4.0.0", out)
        self.assertIn("skills-dev@example-team updated 5.0.0 -> 6.0.0", out)
        versions = {p["id"]: p.get("version") for p in self.final_state()["plugins"]}
        self.assertEqual(versions[REQUIRED[0]], "4.0.0")

    def test_13_current_required_reports_no_update(self):
        self.seed_versions({})
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertNotIn(" updated ", out)

    def test_14_optional_plugins_are_never_updated(self):
        self.seed_versions({})
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        updated = [c for c in fx.shim_log(self.state) if c.startswith("plugin update")]
        self.assertEqual(sorted(updated),
                         sorted("plugin update %s --scope user" % s for s in REQUIRED))

    def test_15_update_failure_warns_and_does_not_fail(self):
        self.seed_versions({})
        code, out = self.run_script(CLAUDE_SHIM_FAIL="update")
        self.assertEqual(code, 0, out)
        self.assertIn("example-team is added but could not be refreshed", out)
        self.assertIn("could not update skills@example-team", out)

    def test_16_disabled_required_is_enabled_then_updated(self):
        self.seed_all(**{REQUIRED[0]: False})
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        log = fx.shim_log(self.state)
        enable = log.index("plugin enable skills@example-team --scope user")
        self.assertGreater(log.index("plugin update skills@example-team --scope user"), enable)

    # --- background auto-update for a marketplace the catalog marks -------------

    def settings_path(self):
        return os.path.join(self.home, ".claude", "settings.json")

    def write_settings(self, data, raw=None):
        os.makedirs(os.path.dirname(self.settings_path()), exist_ok=True)
        with open(self.settings_path(), "w") as handle:
            handle.write(raw if raw is not None else json.dumps(data))

    def settings(self):
        with open(self.settings_path()) as handle:
            return json.load(handle)

    def team_entry(self):
        return self.settings().get("extraKnownMarketplaces", {}).get(TEAM)

    def test_17_fresh_install_turns_auto_update_on(self):
        self.write_settings({"model": "opus", "extraKnownMarketplaces": {}})
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("turned on background updates for example-team", out)
        self.assertIs(self.team_entry().get("autoUpdate"), True)
        self.assertEqual(self.team_entry().get("source"),
                         {"source": "github", "repo": TEAM_REPO})
        self.assertEqual(self.settings().get("model"), "opus")   # other keys survive
        # Only the team marketplace is ours to switch on.
        self.assertNotIn("autoUpdate",
                         self.settings()["extraKnownMarketplaces"].get(OFFICIAL, {}))

    def test_18_existing_entry_keeps_its_source(self):
        self.seed_all()
        source = {"source": "github", "repo": TEAM_REPO, "ref": "main"}
        self.write_settings({"extraKnownMarketplaces": {TEAM: {"source": source}}})
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.team_entry(), {"source": source, "autoUpdate": True})

    def test_19_second_run_reports_on_and_does_not_rewrite(self):
        self.run_script()
        before = os.stat(self.settings_path()).st_mtime_ns
        os.utime(self.settings_path(), ns=(before - 10**9, before - 10**9))
        stamped = os.stat(self.settings_path()).st_mtime_ns
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("example-team updates itself in the background", out)
        self.assertEqual(os.stat(self.settings_path()).st_mtime_ns, stamped)

    def test_20_explicit_off_is_left_alone(self):
        self.seed_all()
        self.write_settings({"extraKnownMarketplaces": {TEAM: {
            "source": {"source": "github", "repo": TEAM_REPO},
            "autoUpdate": False}}})
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIs(self.team_entry().get("autoUpdate"), False)
        self.assertIn("left as you set it", out)

    def test_21_check_writes_nothing(self):
        self.seed_all()
        raw = json.dumps({"model": "opus"})
        self.write_settings(None, raw=raw)
        code, out = self.run_script("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("would turn on background updates for example-team", out)
        with open(self.settings_path()) as handle:
            self.assertEqual(handle.read(), raw)

    def test_22_unreadable_settings_are_not_overwritten(self):
        self.seed_all()
        raw = '{"model": "opus",'   # truncated JSON
        self.write_settings(None, raw=raw)
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("couldn't be read", out)
        with open(self.settings_path()) as handle:
            self.assertEqual(handle.read(), raw)

    def test_23_marketplace_not_added_gets_no_entry(self):
        code, out = self.run_script(github=NO_ACCESS)
        self.assertEqual(code, 1, out)   # required plugins missing, as before
        self.assertIn("isn't added, so it can't update itself", out)
        self.assertIsNone(self.team_entry() if os.path.exists(self.settings_path()) else None)

    def test_24_no_github_login_warns(self):
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("no stored GitHub login found", out)

    def test_25_stored_https_login_is_quiet(self):
        with open(os.path.join(self.home, ".gitconfig"), "w") as handle:
            handle.write("[credential]\n\thelper = \"!f() { echo username=u; echo password=p; }; f\"\n")
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertNotIn("no stored GitHub login found", out)
        self.assertNotIn("password=p", out)   # the credential never reaches output

    def test_26_ssh_login_is_quiet(self):
        greeting = "Hi u! You've successfully authenticated, but GitHub does not provide shell access."
        code, out = self.run_script(TEST_SSH_GREETING=greeting)
        self.assertEqual(code, 0, out)
        self.assertNotIn("no stored GitHub login found", out)

    # --- the settings file a relocated config dir actually reads ---------------

    def test_27_config_dir_gets_the_write(self):
        self.seed_all()
        relocated = os.path.join(self.tmp, "relocated-claude")
        code, out = self.run_script(CLAUDE_CONFIG_DIR=relocated)
        self.assertEqual(code, 0, out)
        with open(os.path.join(relocated, "settings.json")) as handle:
            entry = json.load(handle)["extraKnownMarketplaces"][TEAM]
        self.assertIs(entry.get("autoUpdate"), True)
        if os.path.exists(self.settings_path()):   # the inactive file is not "the" write
            self.assertIsNone(self.team_entry() and self.team_entry().get("autoUpdate"))

    def test_28_registry_opt_out_is_left_alone(self):
        self.seed_all()
        registry = os.path.join(self.home, ".claude", "plugins", "known_marketplaces.json")
        os.makedirs(os.path.dirname(registry), exist_ok=True)
        with open(registry, "w") as handle:
            json.dump({TEAM: {"source": {"source": "github", "repo": TEAM_REPO},
                              "autoUpdate": False}}, handle)
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("left as you set it", out)
        entry = self.team_entry() if os.path.exists(self.settings_path()) else None
        self.assertFalse(entry and entry.get("autoUpdate") is True)

    def test_29_hung_credential_helper_is_bounded(self):
        import time
        with open(os.path.join(self.home, ".gitconfig"), "w") as handle:
            handle.write('[credential]\n\thelper = "!f() { sleep 120; }; f"\n')
        started = time.monotonic()
        code, out = self.run_script()
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, out)
        self.assertIn("the check timed out", out)
        self.assertLess(elapsed, 100)   # catastrophe bound, not a budget: the helper sleeps 120

    # --- the catalog itself ------------------------------------------------------

    def write_catalog(self, data, raw=None):
        path = os.path.join(self.tmp, "bad-catalog.json")
        with open(path, "w") as handle:
            handle.write(raw if raw is not None else json.dumps(data))
        return path

    def assert_refused(self, catalog):
        code, out = self.run_script(catalog=catalog)
        self.assertEqual(code, 1, out)
        self.assertIn("the plugin list is unreadable", out)
        self.assertEqual(fx.shim_log(self.state), [])   # not even a listing

    def test_30_missing_catalog_refuses(self):
        self.assert_refused(os.path.join(self.tmp, "absent.json"))

    def test_31_unparseable_catalog_refuses(self):
        self.assert_refused(self.write_catalog(None, raw='{"plugins": ['))

    def test_32_malformed_entries_refuse(self):
        good = {"marketplaces": [], "plugins": []}
        for bad in (
                {"marketplaces": {}, "plugins": []},
                dict(good, plugins=[{"tier": "maybe", "id": "a@b", "why": "x"}]),
                dict(good, plugins=[{"tier": "optional", "id": "no-marketplace", "why": "x"}]),
                dict(good, plugins=[{"tier": "optional", "id": "a@b", "why": "x|y"}]),
                dict(good, plugins=[{"tier": "optional", "id": "a@b", "why": ""}]),
                dict(good, marketplaces=[{"name": "m", "source": "o/r", "auto_update": "yes"}]),
                dict(good, marketplaces=[{"name": "m"}])):
            with self.subTest(bad=bad):
                self.assert_refused(self.write_catalog(bad))

    def test_33_no_auto_update_marketplace_skips_the_login_probe(self):
        data = json.loads(json.dumps(TEAM_CATALOG))
        data["marketplaces"][1]["auto_update"] = False
        code, out = self.run_script(catalog=self.write_catalog(data))
        self.assertEqual(code, 0, out)
        self.assertNotIn("Auto-update", out)
        self.assertNotIn("GitHub login", out)
        self.assertFalse(os.path.exists(self.ssh_log))
        # Positive control: the team catalog does run it.
        self.run_script()
        self.assertTrue(os.path.exists(self.ssh_log))


class ShippedCatalogTest(unittest.TestCase):
    """What a plain run installs: the official marketplace, optional plugins."""

    def setUp(self):
        with open(SHIPPED_CATALOG, encoding="utf-8") as handle:
            self.catalog = json.load(handle)

    def test_official_marketplace_only(self):
        self.assertEqual([(m["name"], m["source"]) for m in self.catalog["marketplaces"]],
                         [(OFFICIAL, "anthropics/claude-plugins-official")])
        self.assertFalse(any(m.get("auto_update") for m in self.catalog["marketplaces"]))

    def test_every_plugin_is_optional_and_official(self):
        self.assertTrue(self.catalog["plugins"])
        for plugin in self.catalog["plugins"]:
            self.assertEqual(plugin["tier"], "optional")
            self.assertTrue(plugin["id"].endswith("@" + OFFICIAL), plugin["id"])

    def test_plain_run_installs_the_shipped_list(self):
        case = SetupPluginsTest("test_1_everything_installs")
        case.setUp()
        self.addCleanup(case.doCleanups)
        code, out = case.run_script(catalog=None)
        self.assertEqual(code, 0, out)
        user = {p["id"] for p in case.final_state()["plugins"] if p["scope"] == "user"}
        self.assertEqual(user, {p["id"] for p in self.catalog["plugins"]})
        self.assertNotIn("Auto-update", out)


if __name__ == "__main__":
    unittest.main()
