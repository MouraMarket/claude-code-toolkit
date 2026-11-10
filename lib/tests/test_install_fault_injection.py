"""Kill-point sweeps for `--update` and `--rollback`.

Each scenario runs once uninterrupted with the event counter on (the census
and the reference end state), then once per kill point: SIGKILL right after
event k, a parse check on the files a kill could leave half-written, and one
plain re-run that must exit 0 and reach the reference end state.
"""

import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest

LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import fault_inject  # noqa: E402
import install_fixture as fx  # noqa: E402
import install_record  # noqa: E402
from test_install_update import RollbackFixture  # noqa: E402

SKIP_REASON = "fault injection needs POSIX process groups and SIGKILL"


def parses_as_object_or_absent(path):
    if not os.path.lexists(path):
        return True
    try:
        with open(path, encoding="utf-8") as handle:
            return isinstance(json.load(handle), dict)
    except (OSError, ValueError):
        return False


@unittest.skipIf(sys.platform == "win32", SKIP_REASON)
class TestFaultInjection(RollbackFixture):

    def setUp(self):
        super().setUp()
        self.scratch = self.root + ".fault"
        self.start = self.root + ".start"
        os.makedirs(self.scratch)
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.addCleanup(shutil.rmtree, self.start, True)
        self.settings_file = os.path.join(self.home, ".claude", "settings.json")
        self.record_file = install_record.record_path(self.home)

    # Scenarios: each leaves the fixture one command away from the run under test.

    def scenario_u(self):
        self.push_b()
        return ["--update"]

    def scenario_r(self):
        self.update_to_b()
        return ["--rollback"]

    # Running

    def run_fault(self, args, at=None, counter=True):
        """Run install.sh like run_in does; returns (proc, events, log)."""
        env = dict(os.environ)
        env["HOME"] = self.home
        env["USERPROFILE"] = self.home
        env["CLAUDE_TOOLKIT_CONFIG"] = self.origin_config
        for key in [k for k in env if k.startswith("GIT_") or k.startswith("FAULT_")]:
            del env[key]
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        env = fx.shim_env(env, self.bin_dir, self.state)
        env["CLAUDE_TOOLKIT_MANAGED_DIR"] = self.managed
        log = None
        if counter:
            env = fault_inject.install(env, self.home, self.repo, self.scratch)
            log = env["FAULT_LOG"]
            for stale in (log, log + ".kill"):
                if os.path.exists(stale):
                    os.remove(stale)
            if at is not None:
                env["FAULT_AT"] = str(at)
        proc = subprocess.run(
            [os.path.join(self.repo, "install.sh")] + list(args),
            cwd=self.repo, env=env, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=120, universal_newlines=True,
            start_new_session=True)
        return proc, (fault_inject.events(log) if log else []), log

    def restore(self):
        shutil.rmtree(self.root)
        shutil.copytree(self.start, self.root, symlinks=True)

    # State

    def load_json(self, path):
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def end_state(self):
        record = self.load_json(self.record_file)
        if isinstance(record, dict):
            for key in ("installed_at", "first_installed_at"):
                record.pop(key, None)
            rollback = record.get("rollback")
            if isinstance(rollback, dict):
                rollback.pop("recorded_at", None)
                # The value is a timestamp; whether a pin was recorded is not.
                rollback["pinned_at"] = rollback.get("pinned_at") is not None
        bin_dir = os.path.join(self.home, ".local", "bin")
        links = {}
        if os.path.isdir(bin_dir):
            for name in sorted(os.listdir(bin_dir)):
                path = os.path.join(bin_dir, name)
                if os.path.islink(path):
                    links[name] = os.readlink(path)
        plugins = set()
        state = self.load_json(self.state)
        if isinstance(state, dict):
            plugins = set(
                (p.get("id"), p.get("enabled"))
                for p in state.get("plugins", []) if p.get("scope") == "user")
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                             capture_output=True, universal_newlines=True)
        sym = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=self.repo,
                             capture_output=True, universal_newlines=True)
        return {
            "settings": self.load_json(self.settings_file),
            "record": record,
            "links": links,
            "plugins": sorted(plugins, key=repr),
            "head": (rev.stdout.strip(), sym.returncode != 0),
        }

    def parse_failures(self):
        claude_dir = os.path.join(self.home, ".claude")
        paths = [self.settings_file, self.record_file]
        for pattern in ("settings.json.pre-claude-code-toolkit.bak",
                        "settings.json.claude-code-toolkit-*.bak"):
            paths.extend(sorted(glob.glob(os.path.join(claude_dir, pattern))))
        return [p for p in paths if not parses_as_object_or_absent(p)]

    # Census

    def same_path(self, text, target):
        # `detail` is "src -> dst"; the destination is the replaced path.
        dst = text.split(" -> ")[-1]
        return os.path.realpath(dst) == os.path.realpath(target)

    def assert_census(self, events, scenario):
        def has(predicate):
            return any(predicate(kind, detail) for _n, kind, detail in events)

        self.assertTrue(has(lambda k, d: k == "replace"
                            and self.same_path(d, self.settings_file)),
                        "no settings.json replace event")
        self.assertTrue(has(lambda k, d: k == "replace"
                            and self.same_path(d, self.record_file)),
                        "no install-record replace event")
        if scenario == "u":
            git_word, plugin_word = "merge", "install"
        else:
            git_word, plugin_word = "checkout", "uninstall"
        self.assertTrue(has(lambda k, d: k == "git" and git_word in d.split()[1:]),
                        "no git %s event" % git_word)
        self.assertTrue(has(lambda k, d: k == "claude"
                            and d.split()[1:3] == ["plugin", plugin_word]),
                        "no claude plugin %s event" % plugin_word)
        link_kind = "ln" if scenario == "u" else "rm"
        self.assertTrue(has(lambda k, d: k == link_kind and "claude-probe" in d),
                        "no %s event naming the probe link" % link_kind)

    # Sweep

    def assert_killed(self, killed, killed_events, k, log):
        self.assertEqual(
            -signal.SIGKILL, killed.returncode,
            self.out(killed) + "\n" + fault_inject.diagnose(
                killed.returncode, fault_inject.kill_report(log),
                len(killed_events), k))

    def sweep(self, scenario, args):
        began = time.time()
        shutil.copytree(self.root, self.start, symlinks=True)

        self.restore()
        proc, events, _log = self.run_fault(args)
        self.assertEqual(0, proc.returncode, self.out(proc))
        reference = self.end_state()
        total = len(events)
        self.assert_census(events, scenario)

        ln_numbers = [n for n, kind, _d in events if kind == "ln"]
        skipped = set(ln_numbers[1:-1])
        points = [(n, detail) for n, _kind, detail in events if n not in skipped]

        ran = 0
        for k, detail in points:
            with self.subTest(k=k, event=detail):
                ran += 1
                self.restore()
                killed, killed_events, log = self.run_fault(args, at=k)
                self.assert_killed(killed, killed_events, k, log)
                self.assertEqual(k, len(killed_events))
                self.assertEqual([], self.parse_failures())
                again, _none, _log = self.run_fault(args, counter=False)
                self.assertEqual(0, again.returncode, self.out(again))
                self.assertEqual(reference, self.end_state())
        # Counted from the dry run rather than from `points`:
        # every event, less the `ln` events between the first and the last.
        self.assertEqual(total - max(0, len(ln_numbers) - 2), ran)
        print("fault sweep %s: N=%d kill points=%d wall=%.1fs"
              % (scenario, total, len(points), time.time() - began),
              file=sys.stderr)

    def test_update_recovers_from_a_kill_at_every_event(self):
        self.sweep("u", self.scenario_u())

    def test_rollback_recovers_from_a_kill_at_every_event(self):
        self.sweep("r", self.scenario_r())


if __name__ == "__main__":
    unittest.main()
