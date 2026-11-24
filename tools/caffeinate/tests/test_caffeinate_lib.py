"""Tests for the caffeinate hook.

Hermetic: the OS is reached through one injectable `System` — clock included —
so no test here depends on whether this machine is plugged in, which processes
happen to be running, or how long anything takes.

The executable is exercised through subprocess, not just the lib. A sibling tool
in this repo shipped 93 green unit tests with zero lines of any hook executed,
and the suite stayed green through two reintroduced bugs.

ON WHAT CI CAN AND CANNOT PROVE: the tests that spawn a real assertion are
skipped off darwin, deliberately and visibly. `pmset` does not exist on a Linux
runner, so `power_state()` raises, the hook's `except: pass` swallows it, and
every such test would pass having exercised nothing. A green Linux run is not
evidence for those; `install.sh` running the suite on a developer Mac is.
"""

import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO, "lib"))
import caffeinate_lib as lib
import hook_manifest

HOOK = os.path.join(TOOL_DIR, "claude-caffeinate")

CLAUDE_BINARY = (
    "/Users/x/.vscode/extensions/anthropic.claude-code-2.1.233-darwin-arm64"
    "/resources/native-binary/claude")

# The real shape, measured in a VS Code session: hook -> zsh -> claude -> Code.
HOOK_PID = 500
CLAUDE_PID = 300

NOW = 1_700_000_000

# A Bash-tool background shell's real command line, measured 2026-10-20, and a
# hook shell's — the two are both `/bin/zsh` children of claude.
JOB_COMMAND = (
    "/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1792543545638"
    "-tnt82o.sh 2>/dev/null || true && eval 'sleep 90' < /dev/null")
HOOK_SHELL_COMMAND = (
    "/bin/zsh -c \"/Users/x/repo/plugin\"/tools/caffeinate/claude-caffeinate acquire")
JOB_PID = 700

# Read from the module rather than restated, so shortening the throttle cannot
# leave these tests advancing the clock by less than it and passing vacuously.
STAMP_THROTTLE = lib.STAMP_THROTTLE_SECONDS
RENEW_INTERVAL = lib.RENEW_INTERVAL_SECONDS


def process_tree():
    return {
        HOOK_PID: (400, "/usr/bin/python3"),
        400: (CLAUDE_PID, "/bin/zsh"),
        CLAUDE_PID: (200, CLAUDE_BINARY),
        200: (1, "/Applications/Visual Studio Code.app/Contents/MacOS/Code"),
    }


class FakeConfig:
    """Stands in for toolkit_config with a dict, so tests need no real file."""

    def __init__(self, data=None):
        self.data = {"caffeinate": dict(data or {})}

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def get_path(self, *keys, default=None):
        value = self.get(*keys)
        return value if isinstance(value, str) and value else default


class FakeSystem:
    def __init__(self, table=None, on_ac=True, percent=100, first_pid=9000, now=NOW):
        self.table = process_tree() if table is None else dict(table)
        self.on_ac = on_ac
        self.percent = percent
        self.clock = now
        self.spawned = []
        self.terminated = []
        self.table_reads = 0
        self.command_reads = 0
        self.commands = {}
        self.power_reads = 0
        self.terminate_raises = False
        self._next_pid = first_pid

    def now(self):
        return self.clock

    def advance(self, seconds):
        self.clock += seconds

    def process_table(self):
        self.table_reads += 1
        return dict(self.table)

    def command_lines(self, pids):
        self.command_reads += 1
        return {pid: self.commands[pid] for pid in pids if pid in self.commands}

    def start_job(self, pid, command=None):
        """A Bash-tool background job: a snapshot-sourcing shell under claude."""
        self.table[pid] = (CLAUDE_PID, "/bin/zsh")
        self.commands[pid] = command or JOB_COMMAND

    def end_job(self, pid):
        self.table.pop(pid, None)
        self.commands.pop(pid, None)

    def power_state(self):
        self.power_reads += 1
        return self.on_ac, self.percent

    def kill_claude(self):
        """Take the `claude` process out of the tree, as an exit would."""
        self.table.pop(CLAUDE_PID, None)

    def spawn(self, argv):
        self.spawned.append(list(argv))
        pid = self._next_pid
        self._next_pid += 1
        self.table[pid] = (1, "/usr/bin/caffeinate")
        return pid

    def terminate(self, pid):
        if self.terminate_raises:
            raise OSError("no such process")
        self.terminated.append(pid)
        self.table.pop(pid, None)


class CaffeinateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def config(self, **overrides):
        data = {"enabled": True, "state_dir": self.tmp.name}
        data.update(overrides)
        return FakeConfig(data)

    def state_file(self, config, session="s1"):
        return lib.state_path(config, session)

    def recorded(self, config, session="s1"):
        return lib.read_state(self.state_file(config, session))


# --- Parsing and process-tree walking ----------------------------------------

class TestProcessTable(unittest.TestCase):
    def test_command_paths_containing_spaces_survive(self):
        # Splitting naively would truncate this to ".../Code" and break every
        # basename match downstream.
        text = "  200   1 /Applications/Visual Studio Code.app/Contents/MacOS/Code Helper (Plugin)\n"
        table = lib.parse_process_table(text)
        self.assertEqual(
            table[200],
            (1, "/Applications/Visual Studio Code.app/Contents/MacOS/Code Helper (Plugin)"))

    def test_unparseable_and_short_lines_are_skipped_not_fatal(self):
        text = "\n".join([
            "  500   400 /usr/bin/python3",
            "header nonsense here",
            "   9",
            "",
            "  bad  ppid /bin/sh",
        ])
        self.assertEqual(
            lib.parse_process_table(text), {500: (400, "/usr/bin/python3")})


class TestFindClaudePid(unittest.TestCase):
    def test_finds_the_claude_binary_by_basename(self):
        self.assertEqual(lib.find_claude_pid(process_tree(), HOOK_PID), CLAUDE_PID)

    def test_finds_it_on_a_terminal_install_too(self):
        # Both surfaces, as seen in a live `ps -Ao comm=` with a terminal
        # session and VS Code sessions running. `comm` reports the path as
        # INVOKED, not the symlink's target — a terminal install shows
        # ~/.local/bin/claude rather than .../versions/2.1.233, whose basename
        # would have been a version number and matched nothing. Fixtured
        # because the walk failing returns None silently: the hold stays
        # bounded by `-t` and only loses the `-w` backstop, so nothing would
        # visibly break.
        table = dict(process_tree())
        table[CLAUDE_PID] = (200, "/Users/x/.local/bin/claude")
        self.assertEqual(lib.find_claude_pid(table, HOOK_PID), CLAUDE_PID)

    def test_returns_none_when_no_claude_in_the_chain(self):
        table = {500: (400, "/usr/bin/python3"), 400: (1, "/bin/zsh")}
        self.assertIsNone(lib.find_claude_pid(table, 500))

    def test_a_cycle_terminates_rather_than_spinning(self):
        # `ps` reads the table non-atomically and can present one.
        self.assertIsNone(lib.find_claude_pid({10: (11, "/bin/a"), 11: (10, "/bin/b")}, 10))

    def test_a_chain_longer_than_the_limit_gives_up(self):
        table = {n: (n + 1, "/bin/sh") for n in range(2, 40)}
        table[40] = (1, CLAUDE_BINARY)
        self.assertIsNone(lib.find_claude_pid(table, 2, limit=5))

    def test_a_process_named_claude_something_else_does_not_match(self):
        table = {500: (400, "/usr/bin/python3"), 400: (1, "/usr/local/bin/claude-wrapper")}
        self.assertIsNone(lib.find_claude_pid(table, 500))


class TestParsePowerState(unittest.TestCase):
    # `pmset -g ps` output in its real format.
    BATTERY = ("Now drawing from 'Battery Power'\n"
               " -InternalBattery-0 (id=12345678)\t66%; discharging; "
               "10:49 remaining present: true\n")
    AC = ("Now drawing from 'AC Power'\n"
          " -InternalBattery-0 (id=12345678)\t79%; charging; "
          "1:22 remaining present: true\n")

    def test_battery_reports_source_and_charge(self):
        self.assertEqual(lib.parse_power_state(self.BATTERY), (False, 66))

    def test_ac_reports_source_and_charge(self):
        self.assertEqual(lib.parse_power_state(self.AC), (True, 79))

    def test_a_desktop_with_no_battery_line_reports_no_charge(self):
        self.assertEqual(
            lib.parse_power_state("Now drawing from 'AC Power'\n"), (True, None))

    def test_unreadable_output_reads_as_battery_which_holds_less(self):
        # The safe direction: an empty or broken `pmset` must not be mistaken for
        # wall power, because that is the state that holds MORE.
        self.assertEqual(lib.parse_power_state(""), (False, None))

    def test_a_nonsense_percentage_is_dropped_rather_than_believed(self):
        self.assertIsNone(
            lib.parse_power_state("Now drawing from 'AC Power'\n999%;\n")[1])


class TestBatteryFloor(unittest.TestCase):
    def test_defaults_to_twenty(self):
        self.assertEqual(lib.battery_floor_percent(FakeConfig()), 20)

    def test_zero_disables_it_rather_than_falling_back(self):
        # The difference from `_seconds`, and the reason this reader exists.
        # "No floor" is a thing someone can want; a zero deadline is not.
        self.assertEqual(
            lib.battery_floor_percent(FakeConfig({"battery_floor_percent": 0})), 0)

    def test_a_true_is_not_a_one_percent_floor(self):
        # `True` is an `int` in Python and would otherwise read as 1%.
        self.assertEqual(
            lib.battery_floor_percent(FakeConfig({"battery_floor_percent": True})), 20)

    def test_out_of_range_falls_back_rather_than_clamping(self):
        # Clamping 150 to 100 would release the hold the instant it is taken,
        # which is indistinguishable from the feature being broken.
        for value in (150, -1, 101, "twenty", None):
            self.assertEqual(
                lib.battery_floor_percent(FakeConfig({"battery_floor_percent": value})),
                20, value)

    def test_on_ac_there_is_no_floor_at_any_charge(self):
        config = FakeConfig({"battery_floor_percent": 50})
        self.assertIs(lib.below_floor(config, True, 5), False)

    def test_on_battery_at_or_under_the_floor_is_below_it(self):
        config = FakeConfig({"battery_floor_percent": 20})
        self.assertIs(lib.below_floor(config, False, 20), True)
        self.assertIs(lib.below_floor(config, False, 19), True)
        self.assertIs(lib.below_floor(config, False, 21), False)

    def test_an_unknown_charge_is_not_treated_as_flat(self):
        # A machine that cannot report its charge must not have every hold
        # released as though the battery were empty.
        config = FakeConfig({"battery_floor_percent": 20})
        self.assertIs(lib.below_floor(config, False, None), False)

    def test_a_disabled_floor_never_fires_even_at_zero_percent(self):
        config = FakeConfig({"battery_floor_percent": 0})
        self.assertIs(lib.below_floor(config, False, 0), False)


class TestOverrideExpiry(CaffeinateTestCase):
    """`/caffeinate on` lapses after `battery_override_hours` with no battery
    use, so a setting turned on and forgotten cannot hold forever. The clock
    restarts on USE — a hold actually taken on battery — which is the maintainer's
    rule of 2026-08-26: forgetting the setting and not using it are the same
    event."""

    def stamp(self, config, when):
        lib.write_stamp(lib.stamp_path(config), when)

    def test_hours_default_to_twenty_four(self):
        self.assertEqual(lib.override_hours(FakeConfig()), 24)

    def test_zero_means_never_lapses_rather_than_falling_back(self):
        self.assertEqual(
            lib.override_hours(FakeConfig({"battery_override_hours": 0})), 0)

    def test_a_true_is_not_a_one_hour_window(self):
        self.assertEqual(
            lib.override_hours(FakeConfig({"battery_override_hours": True})), 24)

    def test_negative_and_junk_fall_back(self):
        for value in (-1, "day", None):
            self.assertEqual(
                lib.override_hours(FakeConfig({"battery_override_hours": value})),
                24, value)

    def test_state_is_off_when_the_override_was_never_set(self):
        self.assertEqual(lib.override_state(self.config(), NOW), "off")

    def test_state_is_forever_when_hours_is_zero(self):
        config = self.config(require_ac=False, battery_override_hours=0)
        self.assertEqual(lib.override_state(config, NOW), "forever")

    def test_state_is_unstamped_when_nothing_recorded_the_clock(self):
        config = self.config(require_ac=False)
        self.assertEqual(lib.override_state(config, NOW), "unstamped")

    def test_state_is_active_inside_the_window(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        self.stamp(config, NOW - 23 * 3600)
        self.assertEqual(lib.override_state(config, NOW), "active")

    def test_state_is_lapsed_past_the_window(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        self.stamp(config, NOW - 25 * 3600)
        self.assertEqual(lib.override_state(config, NOW), "lapsed")

    def test_the_boundary_is_inclusive_of_the_full_window(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        self.stamp(config, NOW - 24 * 3600)
        self.assertEqual(lib.override_state(config, NOW), "active")
        self.stamp(config, NOW - 24 * 3600 - 1)
        self.assertEqual(lib.override_state(config, NOW), "lapsed")

    def test_an_unreadable_stamp_is_lenient_rather_than_refusing(self):
        # Deliberate. `require_ac: false` set by hand, or a cleared state
        # directory, means the config says yes while nothing says when. Refusing
        # would make a hand-edited config silently inert. Granting starts the
        # clock now, so it buys one window and cannot accumulate.
        config = self.config(require_ac=False)
        path = lib.stamp_path(config)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for content in ("not json", "[1,2]", "{}", '{"last_used_at":"x"}',
                        '{"last_used_at":true}', '{"last_used_at":0}'):
            with open(path, "w") as handle:
                handle.write(content)
            self.assertEqual(lib.override_state(config, NOW), "unstamped", content)

    def test_override_state_never_writes(self):
        # A predicate that stamps as a side effect of being asked is a predicate
        # that cannot be tested, and would make the clock un-lapse-able.
        config = self.config(require_ac=False)
        lib.override_state(config, NOW)
        self.assertFalse(os.path.exists(lib.stamp_path(config)))

    def test_holds_on_battery_is_false_only_for_off_and_lapsed(self):
        # Every config here shares one state_dir, so the stamp is cleared
        # between cases on purpose. Written without that, the lapsed stamp
        # leaked into the unstamped case and it failed for a reason that had
        # nothing to do with the function.
        stamp = lib.stamp_path(self.config())

        self.assertIs(lib.holds_on_battery(self.config(), NOW), False)

        lapsed = self.config(require_ac=False, battery_override_hours=24)
        self.stamp(lapsed, NOW - 100 * 3600)
        self.assertIs(lib.holds_on_battery(lapsed, NOW), False)

        lib.clear_stamp(stamp)
        self.assertIs(
            lib.holds_on_battery(self.config(require_ac=False), NOW), True)
        self.assertIs(
            lib.holds_on_battery(
                self.config(require_ac=False, battery_override_hours=0), NOW),
            True)


class TestOverrideExpiryInAcquire(CaffeinateTestCase):
    def battery(self, percent=80):
        return FakeSystem(on_ac=False, percent=percent)

    def test_a_live_override_holds_on_battery(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(lib.stamp_path(config), NOW - 3600)
        self.assertEqual(
            lib.acquire(config, self.battery(), "s1", HOOK_PID), "held")

    def test_a_lapsed_override_holds_nothing(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(lib.stamp_path(config), NOW - 25 * 3600)
        system = self.battery()
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "on-battery-lapsed")
        self.assertEqual(system.spawned, [])

    def test_lapsing_mid_session_RELEASES_the_running_hold(self):
        # THE ONE THAT MATTERS. If a lapse only stopped NEW holds, a session
        # already running would keep the machine awake indefinitely — which is
        # the entire failure the expiry exists to prevent.
        config = self.config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(lib.stamp_path(config), NOW - 3600)
        system = self.battery()
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        lib.write_stamp(lib.stamp_path(config), NOW - 25 * 3600)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID),
                         "released-battery-lapsed")
        self.assertEqual(system.terminated, [held])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_a_lapsed_override_releases_at_settle_too(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(lib.stamp_path(config), NOW - 3600)
        system = self.battery()
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        lib.write_stamp(lib.stamp_path(config), NOW - 25 * 3600)
        self.assertEqual(
            lib.settle(config, system, "s1", HOOK_PID), "released-battery")
        self.assertEqual(system.terminated, [held])

    def test_holding_on_battery_restarts_the_clock(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(lib.stamp_path(config), NOW - 20 * 3600)
        system = self.battery()
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertEqual(lib.read_stamp(lib.stamp_path(config)), NOW)

    def test_a_lapsed_clock_is_never_restarted_by_the_hook(self):
        # Ordering: the refresh must come AFTER the holds_on_battery gate. If it
        # ran first, every acquire would revive a dead override and the expiry
        # could never fire, while every predicate test still passed.
        config = self.config(require_ac=False, battery_override_hours=24)
        dead = NOW - 25 * 3600
        lib.write_stamp(lib.stamp_path(config), dead)
        lib.acquire(config, self.battery(), "s1", HOOK_PID)
        self.assertEqual(lib.read_stamp(lib.stamp_path(config)), dead)

    def test_wall_power_never_touches_the_clock(self):
        # Working plugged in is not "using the override", so it must not keep it
        # alive — otherwise it would never lapse for anyone who works at a desk.
        config = self.config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(lib.stamp_path(config), NOW - 20 * 3600)
        lib.acquire(config, FakeSystem(on_ac=True), "s1", HOOK_PID)
        self.assertEqual(lib.read_stamp(lib.stamp_path(config)), NOW - 20 * 3600)

    def test_the_clock_is_refreshed_from_the_fast_path_too(self):
        # A session-length hold takes the fast path for an hour at a time. A
        # clock that only advanced on the slow path would lapse mid-use.
        config = self.config(require_ac=False, battery_override_hours=24,
                             hold_seconds=7200)
        system = self.battery()
        lib.acquire(config, system, "s1", HOOK_PID)
        # Age the stamp past the throttle by hand: the renewal interval is
        # shorter than the throttle, so the clock alone cannot get a fast-path
        # call and a due stamp into the same moment.
        lib.write_stamp(lib.stamp_path(config), NOW - STAMP_THROTTLE)
        system.advance(RENEW_INTERVAL // 2)
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "already-held")
        self.assertEqual(
            lib.read_stamp(lib.stamp_path(config)), NOW + RENEW_INTERVAL // 2)

    def test_the_stamp_is_throttled_rather_than_written_every_batch(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        system = self.battery()
        lib.acquire(config, system, "s1", HOOK_PID)
        system.advance(60)          # inside STAMP_THROTTLE_SECONDS
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertEqual(lib.read_stamp(lib.stamp_path(config)), NOW)

    def test_no_clock_is_kept_when_the_override_never_lapses(self):
        config = self.config(require_ac=False, battery_override_hours=0)
        lib.acquire(config, self.battery(), "s1", HOOK_PID)
        self.assertFalse(os.path.exists(lib.stamp_path(config)))

    def test_the_default_config_is_unaffected_by_any_of_this(self):
        # require_ac defaults TRUE, so a machine that never opted in must behave
        # exactly as before: no clock, no stamp, no hold on battery.
        config = self.config()
        system = self.battery()
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "on-battery")
        self.assertFalse(os.path.exists(lib.stamp_path(config)))


class TestIsClaude(unittest.TestCase):
    def test_true_for_the_live_claude_the_hold_was_bound_to(self):
        self.assertIs(lib.is_claude(process_tree(), CLAUDE_PID), True)

    def test_false_once_that_process_has_gone(self):
        table = dict(process_tree())
        del table[CLAUDE_PID]
        self.assertIs(lib.is_claude(table, CLAUDE_PID), False)

    def test_false_when_the_pid_was_reused_by_something_else(self):
        table = dict(process_tree())
        table[CLAUDE_PID] = (1, "/usr/bin/ssh-agent")
        self.assertIs(lib.is_claude(table, CLAUDE_PID), False)

    def test_false_for_an_unbound_hold(self):
        self.assertIs(lib.is_claude(process_tree(), None), False)

    def test_it_agrees_with_the_walk_that_records_the_pid(self):
        # If these two disagreed on what counts as `claude`, a hold could be
        # bound at acquire and unrecognised at settle — the session would sleep
        # five minutes after every turn while every other test still passed.
        for command in (CLAUDE_BINARY, "/Users/x/.local/bin/claude"):
            table = dict(process_tree())
            table[CLAUDE_PID] = (200, command)
            found = lib.find_claude_pid(table, HOOK_PID)
            self.assertEqual(found, CLAUDE_PID, command)
            self.assertIs(lib.is_claude(table, found), True, command)


class TestIsCaffeinate(unittest.TestCase):
    def test_true_only_for_a_caffeinate_process(self):
        self.assertTrue(lib.is_caffeinate({77: (1, "/usr/bin/caffeinate")}, 77))

    def test_false_when_the_pid_was_reused_by_something_else(self):
        # This is the whole reason the check exists: a stale state file must
        # never be a licence to signal whatever inherited that number.
        self.assertFalse(lib.is_caffeinate({77: (1, "/usr/bin/ssh-agent")}, 77))

    def test_false_when_the_pid_is_gone(self):
        self.assertFalse(lib.is_caffeinate({}, 77))


# --- Configuration -----------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_disabled_by_default_so_nobody_inherits_a_wakeful_mac(self):
        self.assertFalse(lib.enabled(FakeConfig()))

    def test_require_ac_defaults_on(self):
        self.assertTrue(lib.require_ac(FakeConfig()))

    def test_non_boolean_flags_fall_back_rather_than_being_truthy(self):
        self.assertFalse(lib.enabled(FakeConfig({"enabled": "yes"})))
        self.assertTrue(lib.require_ac(FakeConfig({"require_ac": "no"})))

    def test_the_settle_window_is_five_minutes_by_default(self):
        self.assertEqual(lib.settle_seconds(FakeConfig()), 300)

    def test_the_idle_timer_is_thirty_minutes_by_default(self):
        # The maintainer's number.
        self.assertEqual(lib.hold_seconds(FakeConfig()), 1800)

    def test_the_job_cap_is_one_hour_by_default(self):
        # The maintainer's number (an earlier 4h cap was judged too long).
        self.assertEqual(lib.job_hold_seconds(FakeConfig()), 3600)

    def test_bad_job_caps_fall_back(self):
        for bad in ("soon", None, True, 0, -5):
            self.assertEqual(
                lib.job_hold_seconds(FakeConfig({"job_hold_seconds": bad})), 3600)
        self.assertEqual(
            lib.job_hold_seconds(FakeConfig({"job_hold_seconds": 600})), 600)

    def test_the_ceiling_comfortably_exceeds_the_settle_window(self):
        # A ceiling below the settle window would make `settle` EXTEND the hold.
        self.assertGreater(lib.hold_seconds(FakeConfig()), lib.settle_seconds(FakeConfig()))

    def test_bad_durations_fall_back_rather_than_producing_a_dead_hold(self):
        for key, default in (("settle_seconds", 300), ("hold_seconds", 7200)):
            for bad in ("soon", None, True, 0, -5, [], {}):
                self.assertEqual(
                    lib._seconds(FakeConfig({key: bad}), key, default), default,
                    "%s=%r should fall back" % (key, bad))

    def test_session_id_is_sanitised_into_a_filename(self):
        self.assertEqual(lib.safe_name("../../etc/passwd"), "------etc-passwd")
        self.assertEqual(lib.safe_name(""), "unknown")
        self.assertEqual(lib.safe_name(None), "unknown")
        self.assertEqual(len(lib.safe_name("x" * 200)), 64)


class TestAssertionArgv(unittest.TestCase):
    def test_ac_gets_the_system_sleep_flag(self):
        self.assertEqual(
            lib.assertion_argv(True, 300, 7200),
            ["caffeinate", "-i", "-s", "-t", "7200", "-w", "300"])

    def test_battery_omits_it_because_the_man_page_says_it_is_invalid(self):
        self.assertEqual(
            lib.assertion_argv(False, 300, 7200),
            ["caffeinate", "-i", "-t", "7200", "-w", "300"])

    def test_never_prevents_display_sleep(self):
        for on_ac in (True, False):
            self.assertNotIn("-d", lib.assertion_argv(on_ac, 300, 600))

    def test_every_hold_carries_a_deadline_even_without_a_claude_pid(self):
        # The v1 bug lived here: no claude pid produced a differently-shaped
        # hold, and the difference was invisible to the next caller.
        self.assertEqual(
            lib.assertion_argv(True, None, 7200),
            ["caffeinate", "-i", "-s", "-t", "7200"])


# --- State on disk -----------------------------------------------------------

class TestState(CaffeinateTestCase):
    def test_round_trip(self):
        path = self.state_file(self.config())
        lib.write_state(path, 4242, NOW + 7200, CLAUDE_PID)
        self.assertEqual(
            lib.read_state(path),
            {"caffeinate_pid": 4242, "expires_at": NOW + 7200,
             "claude_pid": CLAUDE_PID})

    def test_an_unbound_hold_round_trips_as_claude_pid_none(self):
        path = self.state_file(self.config())
        lib.write_state(path, 4242, NOW + 7200, None)
        self.assertIsNone(lib.read_state(path)["claude_pid"])

    def test_a_record_from_the_previous_version_reads_as_unbound(self):
        # THE UPGRADE CASE, and the one with teeth. A hold taken by the installed
        # toolkit minutes before this version lands has no `claude_pid` key at
        # all. Discarding the whole record for that would orphan a live
        # `caffeinate` process that nothing would ever signal again — it would
        # hold until its own `-t` expired, with no state file naming it.
        # Reading it as unbound routes it to settle's five-minute downgrade,
        # which is exactly what the code that wrote it intended.
        path = os.path.join(self.tmp.name, "old.json")
        with open(path, "w") as handle:
            json.dump({"caffeinate_pid": 4242, "expires_at": NOW + 7200}, handle)
        record = lib.read_state(path)
        self.assertIsNotNone(record)
        self.assertEqual(record["caffeinate_pid"], 4242)
        self.assertIsNone(record["claude_pid"])

    def test_a_junk_claude_pid_reads_as_unbound_without_losing_the_record(self):
        # Same asymmetry as above: the bound pid is the OPTIONAL half, so a bad
        # value degrades that field rather than discarding a live assertion.
        path = os.path.join(self.tmp.name, "junk.json")
        for value in ('"abc"', "true", "null", "0", "1", "-5"):
            with open(path, "w") as handle:
                handle.write(
                    '{"caffeinate_pid":4242,"expires_at":%d,"claude_pid":%s}'
                    % (NOW + 7200, value))
            record = lib.read_state(path)
            self.assertIsNotNone(record, value)
            self.assertIsNone(record["claude_pid"], value)

    def test_missing_file_reads_as_no_hold(self):
        self.assertIsNone(lib.read_state(os.path.join(self.tmp.name, "nope.json")))

    def test_garbage_file_reads_as_no_hold_rather_than_raising(self):
        path = os.path.join(self.tmp.name, "bad.json")
        for content in ("not json at all", "[1,2,3]", "{}",
                        '{"caffeinate_pid":"abc","expires_at":1}',
                        '{"caffeinate_pid":true,"expires_at":1}',
                        '{"caffeinate_pid":9000}',
                        '{"expires_at":1}',
                        '{"caffeinate_pid":1,"expires_at":1}'):
            with open(path, "w") as handle:
                handle.write(content)
            self.assertIsNone(lib.read_state(path), content)


# --- acquire -----------------------------------------------------------------

class TestAcquire(CaffeinateTestCase):
    def test_disabled_spawns_nothing(self):
        config = self.config(enabled=False)
        system = FakeSystem()
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "disabled")
        self.assertEqual(system.spawned, [])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_on_battery_spawns_nothing_by_default(self):
        system = FakeSystem(on_ac=False)
        self.assertEqual(lib.acquire(self.config(), system, "s1", HOOK_PID), "on-battery")
        self.assertEqual(system.spawned, [])

    def test_unplugging_mid_session_releases_the_running_hold(self):
        # "Only while plugged in" has to cover the hold already running, or the
        # answer depends on whether you unplugged before or after the turn began.
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        system.on_ac = False
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "released-battery")
        self.assertEqual(system.terminated, [held])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_on_battery_with_require_ac_off_holds_without_the_s_flag(self):
        config = self.config(require_ac=False, hold_seconds=7200)
        system = FakeSystem(on_ac=False)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        self.assertEqual(
            system.spawned, [["caffeinate", "-i", "-t", "7200", "-w", str(CLAUDE_PID)]])

    def test_on_ac_takes_a_bounded_hold_bound_to_the_claude_pid(self):
        config = self.config(hold_seconds=7200)
        system = FakeSystem()
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        self.assertEqual(
            system.spawned,
            [["caffeinate", "-i", "-s", "-t", "7200", "-w", str(CLAUDE_PID)]])
        self.assertEqual(
            self.recorded(config),
            {"caffeinate_pid": 9000, "expires_at": NOW + 7200,
             "claude_pid": CLAUDE_PID})

    def test_no_claude_pid_still_records_the_same_deadline(self):
        config = self.config(hold_seconds=7200)
        system = FakeSystem(table={HOOK_PID: (1, "/usr/bin/python3")})
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        self.assertEqual(system.spawned, [["caffeinate", "-i", "-s", "-t", "7200"]])
        self.assertEqual(self.recorded(config)["expires_at"], NOW + 7200)

    def test_a_later_acquire_never_inherits_an_earlier_turns_deadline(self):
        # THE v1 REGRESSION. A hold taken without a claude pid was recorded as
        # if it had no deadline, so the next acquire returned "already-held" and
        # ran a fresh turn under the previous turn's clock — the machine slept
        # mid-build. Covered on the no-claude-pid path specifically, because
        # that is the one that was wrong.
        config = self.config(hold_seconds=7200)
        system = FakeSystem(table={HOOK_PID: (1, "/usr/bin/python3")})
        lib.acquire(config, system, "s1", HOOK_PID)
        first = self.recorded(config)

        system.advance(5000)  # past the refresh point, still inside the old hold
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        second = self.recorded(config)

        self.assertEqual(second["expires_at"], NOW + 5000 + 7200)
        self.assertGreater(second["expires_at"], first["expires_at"])
        self.assertIn(first["caffeinate_pid"], system.terminated)

    def test_a_fresh_hold_is_not_renewed_and_costs_one_table_read(self):
        # The fast path spawns nothing. It does read the process table once —
        # the job scan needs it on every call — and only once: the session hold
        # and the scan share it, which is what TestTimeoutBudget's sum assumes.
        config = self.config(hold_seconds=7200)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        reads_after_first = system.table_reads

        system.advance(60)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "already-held")
        self.assertEqual(len(system.spawned), 1)
        self.assertEqual(system.table_reads, reads_after_first + 1)
        self.assertEqual(system.command_reads, 0)   # no candidate shells

    def test_a_hold_older_than_the_renew_interval_is_renewed(self):
        config = self.config(hold_seconds=7200)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        system.advance(RENEW_INTERVAL + 1)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        self.assertEqual(len(system.spawned), 2)
        self.assertEqual(
            self.recorded(config)["expires_at"], NOW + RENEW_INTERVAL + 1 + 7200)

    def test_the_deadline_runs_from_the_LAST_activity(self):
        # THE 2026-10-20 FIX. The old rule renewed only past half-life, so after
        # three busy hours the deadline sat one hour past the last activity,
        # not two — the machine slept on a per-session block, not an idle timer.
        config = self.config(hold_seconds=7200)
        system = FakeSystem()
        for _ in range(36):                 # a tool batch every 5 min for 3h
            lib.acquire(config, system, "s1", HOOK_PID)
            system.advance(300)
        system.advance(-300)
        last_activity = system.now()

        expires_at = self.recorded(config)["expires_at"]
        self.assertGreater(expires_at, last_activity + 7200 - RENEW_INTERVAL)
        self.assertLessEqual(expires_at, last_activity + 7200)

    def test_a_small_ceiling_still_renews_before_it_expires(self):
        # The interval is capped at half the ceiling. The ceiling here must be
        # under 2 * RENEW_INTERVAL for the cap to bind at all — at 300s it did
        # not, and an uncapped mutant survived.
        ceiling = RENEW_INTERVAL * 2 - 40
        config = self.config(hold_seconds=ceiling)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        system.advance(ceiling // 2 + 1)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")

    def test_renewal_spawns_the_new_hold_before_dropping_the_old(self):
        # Drop-then-spawn leaves a moment with no assertion, and on a machine
        # whose idle timer already ran out that moment is enough to sleep it.
        config = self.config(hold_seconds=7200)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        old = self.recorded(config)["caffeinate_pid"]
        spawned_at_terminate = []
        real_terminate = system.terminate

        def terminate(pid):
            spawned_at_terminate.append(len(system.spawned))
            real_terminate(pid)
        system.terminate = terminate

        system.advance(RENEW_INTERVAL + 1)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        self.assertEqual(system.terminated, [old])
        self.assertEqual(spawned_at_terminate, [2])

    def test_a_dead_recorded_hold_is_replaced_not_trusted(self):
        config = self.config()
        system = FakeSystem()
        lib.write_state(self.state_file(config), 4242, NOW - 1)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        self.assertEqual(system.terminated, [])
        self.assertEqual(len(system.spawned), 1)

    def test_a_reused_pid_is_never_signalled(self):
        config = self.config()
        system = FakeSystem()
        system.table[4242] = (1, "/usr/bin/ssh-agent")
        lib.write_state(self.state_file(config), 4242, NOW - 1)
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertEqual(system.terminated, [])

    def test_sessions_do_not_share_a_hold(self):
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertEqual(lib.acquire(config, system, "s2", HOOK_PID), "held")
        self.assertEqual(len(system.spawned), 2)

    def test_an_unbound_hold_records_claude_pid_as_none(self):
        config = self.config()
        system = FakeSystem(table={HOOK_PID: (1, "/usr/bin/python3")})
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertIsNone(self.recorded(config)["claude_pid"])


class TestJobHolds(CaffeinateTestCase):
    """A background job gets its own hold, bound to the job, capped at 1h."""

    def job_records(self, config):
        return sorted(n for n in os.listdir(self.tmp.name) if lib.JOB_RECORD_INFIX in n)

    def test_a_background_job_gets_a_hold_bound_to_it(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held+1-jobs")
        self.assertIn(
            ["caffeinate", "-i", "-s", "-t", "3600", "-w", str(JOB_PID)], system.spawned)
        self.assertEqual(
            lib.read_state(lib.job_path(config, "s1", JOB_PID))["expires_at"], NOW + 3600)

    def test_it_is_found_on_the_fast_path_too(self):
        # A job is launched BY a tool batch, nearly always under a fresh hold.
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        system.start_job(JOB_PID)
        system.advance(5)
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "already-held+1-jobs")

    def test_on_battery_the_job_hold_omits_dash_s(self):
        config = self.config(require_ac=False)
        system = FakeSystem(on_ac=False, percent=80)
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertIn(["caffeinate", "-i", "-t", "3600", "-w", str(JOB_PID)], system.spawned)

    def test_a_parallel_hook_shell_is_not_a_job(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID, command=HOOK_SHELL_COMMAND)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        self.assertEqual(self.job_records(config), [])

    def test_this_hooks_own_shell_is_never_a_job(self):
        # Even if its command line looked like one: excluded by ancestry.
        config = self.config()
        system = FakeSystem()
        system.commands[400] = JOB_COMMAND
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")
        self.assertEqual(self.job_records(config), [])

    def test_a_shell_under_another_claude_is_not_this_sessions_job(self):
        config = self.config()
        system = FakeSystem()
        system.table[650] = (1, "/usr/local/bin/claude")
        system.table[JOB_PID] = (650, "/bin/zsh")
        system.commands[JOB_PID] = JOB_COMMAND
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")

    def test_a_held_job_is_not_held_twice(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        spawned = len(system.spawned)
        system.advance(30)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "already-held")
        self.assertEqual(len(system.spawned), spawned)

    def test_the_cap_holds_even_through_renewals_and_sweeps(self):
        # THE CEILING TEST. A job still running past its hour must not be re-held —
        # not by the next acquire, and not after a sweep on the slow path
        # clears expired records, which is how a cap quietly becomes "forever".
        config = self.config(hold_seconds=1800)
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        job_hold = lib.read_state(lib.job_path(config, "s1", JOB_PID))["caffeinate_pid"]

        system.advance(3601)
        system.table.pop(job_hold)          # `-t` ended it, as the kernel would
        verdict = lib.acquire(config, system, "s1", HOOK_PID)   # slow path + sweep
        self.assertEqual(verdict, "held")
        system.advance(10)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "already-held")
        job_spawns = [a for a in system.spawned if a[-2:] == ["-w", str(JOB_PID)]]
        self.assertEqual(len(job_spawns), 1)

    def test_a_reused_pid_inside_the_cap_gets_its_own_hold(self):
        # The job exits (its `-w` assertion goes with it) and a NEW job takes
        # the same PID before any sweep clears the old record. The record must
        # not stand in for the new job's hold.
        config = self.config(hold_seconds=1800)
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        old_hold = lib.read_state(lib.job_path(config, "s1", JOB_PID))["caffeinate_pid"]

        system.end_job(JOB_PID)
        system.table.pop(old_hold)          # `-w` ended it with the job
        system.start_job(JOB_PID)           # the PID is reused
        system.advance(1)
        lib.acquire(config, system, "s1", HOOK_PID)
        job_spawns = [a for a in system.spawned if a[-2:] == ["-w", str(JOB_PID)]]
        self.assertEqual(len(job_spawns), 2)

    def test_a_finished_jobs_record_is_swept_once_expired(self):
        config = self.config(hold_seconds=1800)
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        job_hold = lib.read_state(lib.job_path(config, "s1", JOB_PID))["caffeinate_pid"]

        system.end_job(JOB_PID)
        system.table.pop(job_hold)          # `-w` ended it
        system.advance(3601)
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertEqual(self.job_records(config), [])

    def test_release_drops_job_holds_too(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        job_hold = lib.read_state(lib.job_path(config, "s1", JOB_PID))["caffeinate_pid"]
        self.assertEqual(lib.release(config, system, "s1"), "released")
        self.assertIn(job_hold, system.terminated)
        self.assertEqual(self.job_records(config), [])

    def test_unplugging_drops_job_holds(self):
        config = self.config()                      # require_ac defaults true
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        job_hold = lib.read_state(lib.job_path(config, "s1", JOB_PID))["caffeinate_pid"]
        system.on_ac = False
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertIn(job_hold, system.terminated)
        self.assertEqual(self.job_records(config), [])

    def test_the_battery_floor_drops_job_holds_at_settle(self):
        config = self.config(require_ac=False, battery_floor_percent=20)
        system = FakeSystem(on_ac=False, percent=60)
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        job_hold = lib.read_state(lib.job_path(config, "s1", JOB_PID))["caffeinate_pid"]
        system.percent = 15
        self.assertEqual(
            lib.settle(config, system, "s1", HOOK_PID), "released-battery-floor")
        self.assertIn(job_hold, system.terminated)

    def test_the_floor_drops_a_job_hold_after_the_session_hold_expired(self):
        # Codex's repro, 2026-10-20: THE case this tool serves — the session
        # went quiet for 30 minutes, the job carried on, then the battery fell.
        config = self.config(require_ac=False, battery_floor_percent=20)
        system = FakeSystem(on_ac=False, percent=60)
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        session_hold = self.recorded(config)["caffeinate_pid"]
        job_hold = lib.read_state(lib.job_path(config, "s1", JOB_PID))["caffeinate_pid"]

        system.advance(1801)
        system.table.pop(session_hold)      # `-t` ended the idle timer
        system.percent = 15
        self.assertEqual(
            lib.settle(config, system, "s1", HOOK_PID), "released-battery-floor")
        self.assertIn(job_hold, system.terminated)
        self.assertEqual(self.job_records(config), [])

    def test_disabling_drops_job_holds_with_no_session_record(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        lib.clear_state(self.state_file(config))
        self.assertEqual(
            lib.settle(self.config(enabled=False), system, "s1", HOOK_PID),
            "released-disabled")
        self.assertEqual(self.job_records(config), [])

    def test_settle_with_only_a_live_job_hold_leaves_it(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        lib.clear_state(self.state_file(config))
        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "stale")
        self.assertEqual(system.terminated, [])
        self.assertEqual(len(self.job_records(config)), 1)

    def test_settle_leaves_job_holds_alone_in_a_live_session(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "session-alive")
        self.assertEqual(system.terminated, [])

    def test_release_reaches_jobs_even_without_a_session_record(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        lib.clear_state(self.state_file(config))
        self.assertEqual(lib.release(config, system, "s1"), "released")
        self.assertEqual(self.job_records(config), [])

    def test_another_sessions_jobs_are_not_released(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        lib.release(config, system, "s2")
        self.assertEqual(len(self.job_records(config)), 1)

    def test_a_failing_scan_does_not_cost_the_session_hold(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)

        def broken(pids):
            raise OSError("ps hung")
        system.command_lines = broken
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")

    def test_status_reports_job_holds_as_jobs(self):
        config = self.config()
        system = FakeSystem()
        system.start_job(JOB_PID)
        lib.acquire(config, system, "s1", HOOK_PID)
        rows = lib.held_sessions(config, system)
        self.assertEqual(sorted(r["job_pid"] for r in rows if r["job_pid"]), [JOB_PID])
        self.assertEqual({r["session"] for r in rows}, {"s1"})


@unittest.skipUnless(sys.platform == "darwin", "runs the real /bin/ps")
class TestPsShim(unittest.TestCase):
    """The executable tests' `ps` shim must blind the job scan AND nothing else."""

    def run_shim(self, *args):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        holder = type("Holder", (), {"tmp": tmp})()
        shim = TestHookExecutable.shim_ps(holder)
        return subprocess.run([os.path.join(shim, "ps")] + list(args),
                              capture_output=True, text=True, timeout=10).stdout

    def test_it_hides_command_lines(self):
        self.assertEqual(self.run_shim("-o", "pid=,args=", "-p", str(os.getpid())), "")

    def test_it_passes_the_process_table_through(self):
        # Positive control: the real `ps` answers this with our own pid.
        self.assertIn(str(os.getpid()), self.run_shim("-Ao", "pid=,ppid=,comm="))


@unittest.skipUnless(sys.platform == "darwin", "reads the real process table")
class TestRealCommandLines(unittest.TestCase):
    def test_reads_this_processs_command_line(self):
        lines = lib.System().command_lines([os.getpid()])
        self.assertIn("test_caffeinate_lib", lines.get(os.getpid(), ""))

    def test_an_exited_pid_is_simply_absent(self):
        child = subprocess.Popen(["true"])
        child.wait()
        self.assertEqual(lib.System().command_lines([child.pid]), {})


class TestAcquireBatteryFloor(CaffeinateTestCase):
    def battery(self, percent):
        return FakeSystem(on_ac=False, percent=percent)

    def test_below_the_floor_holds_nothing(self):
        config = self.config(require_ac=False, battery_floor_percent=20)
        system = self.battery(15)
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "on-battery-floor")
        self.assertEqual(system.spawned, [])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_above_the_floor_holds_normally(self):
        config = self.config(require_ac=False, battery_floor_percent=20)
        system = self.battery(55)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")

    def test_crossing_the_floor_releases_the_running_hold(self):
        config = self.config(require_ac=False, battery_floor_percent=20)
        system = self.battery(55)
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        system.percent = 12
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "released-battery-floor")
        self.assertEqual(system.terminated, [held])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_the_floor_is_checked_BEFORE_the_fast_path(self):
        # THE ORDERING TEST, and the reason it has its own name. Holds are bound
        # to the session now rather than to a turn, so the fast path can be taken
        # for an hour at a stretch. A floor checked after it would let a laptop
        # run flat while every invocation returned "already-held" and looked
        # correct. Mutating the order in the source must fail HERE.
        config = self.config(require_ac=False, battery_floor_percent=20,
                             hold_seconds=7200)
        system = self.battery(55)
        lib.acquire(config, system, "s1", HOOK_PID)

        system.percent = 5
        system.advance(60)  # well inside the fast path's window
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "released-battery-floor")

    def test_a_disabled_floor_holds_at_any_charge(self):
        config = self.config(require_ac=False, battery_floor_percent=0)
        system = self.battery(2)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")

    def test_the_floor_does_not_apply_on_wall_power(self):
        # A charge below the floor while plugged in is a laptop that is charging.
        config = self.config(battery_floor_percent=20)
        system = FakeSystem(on_ac=True, percent=8)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")

    def test_require_ac_still_wins_over_the_floor(self):
        # Default config on battery: the answer is "on-battery", not a floor
        # verdict, whatever the charge. Two independent reasons to decline must
        # not report each other's reason.
        config = self.config(battery_floor_percent=20)
        system = self.battery(5)
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "on-battery")


# --- settle and release ------------------------------------------------------

class TestSettleWithALiveSession(CaffeinateTestCase):
    """The change of 2026-08-26. `Stop` used to pull every hold in to five
    minutes; now a hold bound to a living `claude` is left alone, because that
    process exiting is what releases it and five minutes does not cover a
    background job that outlives the turn."""

    def test_a_bound_hold_is_left_completely_alone(self):
        config = self.config(hold_seconds=7200, settle_seconds=300)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        before = self.recorded(config)
        spawns = len(system.spawned)

        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "session-alive")
        self.assertEqual(system.terminated, [])
        self.assertEqual(len(system.spawned), spawns)
        self.assertEqual(self.recorded(config), before)

    def test_the_deadline_is_not_shortened(self):
        # Stated separately from the record comparison above, because a
        # mutation that rewrote the record with the SAME pid but a shorter
        # deadline is the specific regression this branch exists to prevent.
        config = self.config(hold_seconds=7200, settle_seconds=300)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertEqual(self.recorded(config)["expires_at"], NOW + 7200)
        lib.settle(config, system, "s1", HOOK_PID)
        self.assertEqual(self.recorded(config)["expires_at"], NOW + 7200)

    def test_repeated_turns_never_erode_the_hold(self):
        # The real shape of a working session: many turns, many settles.
        config = self.config(hold_seconds=7200, settle_seconds=300)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        for _ in range(5):
            system.advance(30)
            self.assertEqual(
                lib.settle(config, system, "s1", HOOK_PID), "session-alive")
        self.assertEqual(self.recorded(config)["expires_at"], NOW + 7200)
        self.assertEqual(system.terminated, [])

    def test_once_claude_exits_the_hold_settles_as_before(self):
        # `-w` would already have dropped the real assertion here. This is the
        # bookkeeping catching up, and it is what stops a dead session's record
        # keeping `session-alive` true forever.
        config = self.config(hold_seconds=7200, settle_seconds=300)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        system.kill_claude()
        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "settling")
        self.assertEqual(system.terminated, [held])
        self.assertEqual(self.recorded(config)["expires_at"], NOW + 300)

    def test_a_reused_claude_pid_does_not_keep_the_hold_alive(self):
        config = self.config(hold_seconds=7200, settle_seconds=300)
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        system.table[CLAUDE_PID] = (1, "/usr/bin/ssh-agent")
        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "settling")


class TestSettleWithoutABoundProcess(CaffeinateTestCase):
    """The walk up to `claude` can legitimately fail, and that hold has nothing
    but its deadline. It keeps the old five-minute downgrade — the asymmetry is
    the point, and it is why `settle` still exists at all."""

    def unbound(self):
        return FakeSystem(table={HOOK_PID: (1, "/usr/bin/python3")})

    def test_pulls_the_deadline_in_to_the_settle_window(self):
        config = self.config(hold_seconds=7200, settle_seconds=300)
        system = self.unbound()
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "settling")
        self.assertEqual(system.terminated, [held])
        self.assertEqual(self.recorded(config)["expires_at"], NOW + 300)

    def test_the_shortened_hold_carries_no_w_flag(self):
        # THE FIXTURE IS THE ASSERTION HERE. `claude` is deliberately PRESENT in
        # the process table while the record is unbound — the shape an
        # old-format record takes inside a healthy session, and the only shape
        # that can tell "does not re-bind" apart from "had nothing to bind to".
        #
        # Written first with an empty tree, where the walk returns None anyway:
        # the mutation that re-walks and re-binds survived, because the fixture
        # made both versions behave identically. A mutation that fails to fail
        # is a finding about the test.
        config = self.config(hold_seconds=7200, settle_seconds=300)
        system = FakeSystem()  # full tree, claude alive at CLAUDE_PID
        lib.write_state(self.state_file(config), 9000, NOW + 7200, None)
        system.table[9000] = (1, "/usr/bin/caffeinate")

        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "settling")
        self.assertEqual(
            system.spawned[-1], ["caffeinate", "-i", "-s", "-t", "300"])
        self.assertIsNone(self.recorded(config)["claude_pid"])

    def test_an_unbound_record_reaches_this_branch_even_with_claude_alive(self):
        # States the premise the test above rests on, separately, so that if the
        # premise ever stops holding it fails by name rather than by silently
        # making the other test vacuous again.
        config = self.config()
        system = FakeSystem()
        lib.write_state(self.state_file(config), 9000, NOW + 7200, None)
        system.table[9000] = (1, "/usr/bin/caffeinate")
        self.assertIs(lib.is_claude(system.table, CLAUDE_PID), True)
        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "settling")

    def test_settling_twice_does_not_extend_the_deadline(self):
        config = self.config()
        system = self.unbound()
        lib.acquire(config, system, "s1", HOOK_PID)
        lib.settle(config, system, "s1", HOOK_PID)
        deadline = self.recorded(config)["expires_at"]

        system.advance(60)
        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "already-settling")
        self.assertEqual(self.recorded(config)["expires_at"], deadline)


class TestSettleReleases(CaffeinateTestCase):
    """Three conditions must end a hold that a live session would otherwise
    keep. Each is checked BEFORE the `session-alive` branch, and each is
    asserted against a BOUND hold specifically — against an unbound one they
    would pass whether or not the ordering were right."""

    def test_disabled_mid_session_releases_instead_of_holding(self):
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        self.assertEqual(
            lib.settle(self.config(enabled=False), system, "s1", HOOK_PID),
            "released-disabled")
        self.assertEqual(system.terminated, [held])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_unplugged_at_settle_releases_rather_than_holding(self):
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        system.on_ac = False
        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "released-battery")
        self.assertEqual(system.terminated, [held])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_crossing_the_battery_floor_releases_rather_than_holding(self):
        config = self.config(require_ac=False, battery_floor_percent=20)
        system = FakeSystem(on_ac=False, percent=55)
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        system.percent = 10
        self.assertEqual(
            lib.settle(config, system, "s1", HOOK_PID), "released-battery-floor")
        self.assertEqual(system.terminated, [held])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_nothing_held_spawns_nothing_and_costs_no_subprocess(self):
        # Settle only ever ends or shortens an existing hold. Taking one here
        # would keep the Mac awake after a turn that never asked for it — and it
        # runs at the end of every turn, so it also must not pay for a process
        # table or a `pmset` read when there is nothing recorded.
        system = FakeSystem()
        self.assertEqual(
            lib.settle(self.config(), system, "s1", HOOK_PID), "nothing-held")
        self.assertEqual(system.spawned, [])
        self.assertEqual(system.table_reads, 0)
        self.assertEqual(system.power_reads, 0)

    def test_a_stale_record_is_cleared_without_signalling_anything(self):
        config = self.config()
        system = FakeSystem()
        system.table[4242] = (1, "/usr/bin/ssh-agent")
        lib.write_state(self.state_file(config), 4242, NOW + 7200, CLAUDE_PID)
        self.assertEqual(lib.settle(config, system, "s1", HOOK_PID), "stale")
        self.assertEqual(system.terminated, [])
        self.assertFalse(os.path.exists(self.state_file(config)))


class TestRelease(CaffeinateTestCase):
    def test_drops_the_hold_and_the_record(self):
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        self.assertEqual(lib.release(config, system, "s1"), "released")
        self.assertEqual(system.terminated, [held])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_releases_even_when_the_feature_is_disabled(self):
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertEqual(lib.release(self.config(enabled=False), system, "s1"), "released")
        self.assertEqual(len(system.terminated), 1)

    def test_nothing_held_is_not_an_error(self):
        self.assertEqual(lib.release(self.config(), FakeSystem(), "s1"), "nothing-held")

    def test_a_failed_signal_still_clears_the_record(self):
        # Leaving the record would make the next acquire think a hold exists.
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        system.terminate_raises = True
        self.assertEqual(lib.release(config, system, "s1"), "stale")
        self.assertFalse(os.path.exists(self.state_file(config)))


# --- The slash command's half ------------------------------------------------

class TestApplyBatteryOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")

    def write(self, data):
        with open(self.path, "w") as handle:
            json.dump(data, handle, indent=2)

    def read(self):
        with open(self.path) as handle:
            return json.load(handle)

    def test_on_sets_require_ac_false(self):
        self.write({"caffeinate": {"enabled": True, "require_ac": True}})
        ok, _ = lib.apply_battery_override(self.path, True)
        self.assertTrue(ok)
        self.assertIs(self.read()["caffeinate"]["require_ac"], False)

    def test_on_also_enables_because_otherwise_it_silently_does_nothing(self):
        # `enabled` defaults FALSE. Setting only `require_ac` would report
        # success and hold nothing, forever, with the config looking correct —
        # the exact silent-failure shape this repo gates on.
        self.write({"caffeinate": {}})
        lib.apply_battery_override(self.path, True)
        section = self.read()["caffeinate"]
        self.assertIs(section["enabled"], True)
        self.assertIs(section["require_ac"], False)

    def test_off_restores_ac_only_without_disabling_the_feature(self):
        # The maintainer's correction of 2026-08-26: plugged-in holding must survive
        # `/caffeinate off`, so `off` is NOT the inverse of `on`.
        self.write({"caffeinate": {"enabled": True, "require_ac": False}})
        lib.apply_battery_override(self.path, False)
        section = self.read()["caffeinate"]
        self.assertIs(section["require_ac"], True)
        self.assertIs(section["enabled"], True)

    def test_off_does_not_enable_a_feature_that_was_never_on(self):
        self.write({"caffeinate": {"enabled": False}})
        lib.apply_battery_override(self.path, False)
        self.assertIs(self.read()["caffeinate"]["enabled"], False)

    def test_other_tools_keys_and_comment_keys_survive_the_write(self):
        # The file is hand-edited and full of `//` documentation keys. A writer
        # that rebuilt the document would silently delete another tool's config.
        self.write({
            "//": "generated by install.sh",
            "owner_name": "fixture-owner",
            "danger_guard": {"extra_safe_roots": ["~/.cache"]},
            "caffeinate": {"//enabled": "why this exists", "enabled": True},
        })
        lib.apply_battery_override(self.path, True)
        data = self.read()
        self.assertEqual(data["//"], "generated by install.sh")
        self.assertEqual(data["owner_name"], "fixture-owner")
        self.assertEqual(data["danger_guard"], {"extra_safe_roots": ["~/.cache"]})
        self.assertEqual(data["caffeinate"]["//enabled"], "why this exists")

    def test_key_order_is_preserved_so_the_file_stays_diffable(self):
        self.write({"a": 1, "caffeinate": {"enabled": True}, "z": 2})
        lib.apply_battery_override(self.path, True)
        self.assertEqual(list(self.read()), ["a", "caffeinate", "z"])

    def test_reports_what_it_changed(self):
        self.write({"caffeinate": {"enabled": True, "require_ac": True}})
        _, changes = lib.apply_battery_override(self.path, True)
        self.assertEqual(changes, [("require_ac", True, False)])

    def test_reports_nothing_changed_when_already_in_that_state(self):
        self.write({"caffeinate": {"enabled": True, "require_ac": False}})
        ok, changes = lib.apply_battery_override(self.path, True)
        self.assertTrue(ok)
        self.assertEqual(changes, [])

    def test_a_missing_caffeinate_section_is_created(self):
        self.write({"owner_name": "fixture-owner"})
        ok, _ = lib.apply_battery_override(self.path, True)
        self.assertTrue(ok)
        self.assertIs(self.read()["caffeinate"]["require_ac"], False)

    def test_a_non_object_caffeinate_section_is_replaced_not_crashed_on(self):
        self.write({"caffeinate": "nonsense"})
        ok, _ = lib.apply_battery_override(self.path, True)
        self.assertTrue(ok)
        self.assertIs(self.read()["caffeinate"]["require_ac"], False)

    def test_an_unparseable_config_refuses_rather_than_overwriting_it(self):
        # THE ONE THAT MATTERS. Rewriting a file we could not parse would
        # replace a hand-written config with a two-line document.
        with open(self.path, "w") as handle:
            handle.write("{ this is not json,,, ")
        ok, changes = lib.apply_battery_override(self.path, True)
        self.assertFalse(ok)
        self.assertEqual(changes, [])
        with open(self.path) as handle:
            self.assertEqual(handle.read(), "{ this is not json,,, ")

    def test_a_json_array_is_refused_too(self):
        self.write([1, 2, 3])
        ok, _ = lib.apply_battery_override(self.path, True)
        self.assertFalse(ok)

    def test_a_missing_config_file_is_refused_rather_than_invented(self):
        # Creating one here would produce a config with no workspace_root, which
        # is what every safety default in this toolkit keys off.
        missing = os.path.join(self.tmp.name, "nope", "config.json")
        ok, _ = lib.apply_battery_override(missing, True)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(missing))

    def test_the_written_file_reparses(self):
        self.write({"caffeinate": {"enabled": True}})
        lib.apply_battery_override(self.path, True)
        lib.apply_battery_override(self.path, False)
        self.assertIs(self.read()["caffeinate"]["require_ac"], True)


class TestHeldSessions(CaffeinateTestCase):
    def test_an_empty_state_directory_reports_nothing(self):
        self.assertEqual(lib.held_sessions(self.config(), FakeSystem()), [])

    def test_a_missing_state_directory_is_not_an_error(self):
        config = self.config(state_dir=os.path.join(self.tmp.name, "nope"))
        self.assertEqual(lib.held_sessions(config, FakeSystem()), [])

    def test_a_live_bound_hold_is_reported_as_both(self):
        config = self.config()
        system = FakeSystem()
        lib.acquire(config, system, "s1", HOOK_PID)
        rows = lib.held_sessions(config, system)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "s1")
        self.assertIs(rows[0]["live"], True)
        self.assertIs(rows[0]["bound"], True)
        self.assertEqual(rows[0]["claude_pid"], CLAUDE_PID)

    def test_an_unbound_hold_is_live_but_not_bound(self):
        config = self.config()
        system = FakeSystem(table={HOOK_PID: (1, "/usr/bin/python3")})
        lib.acquire(config, system, "s1", HOOK_PID)
        rows = lib.held_sessions(config, system)
        self.assertIs(rows[0]["live"], True)
        self.assertIs(rows[0]["bound"], False)

    def test_a_stale_record_is_reported_rather_than_hidden(self):
        # A stale record is how a hold silently stops being real, so it is the
        # single most useful thing for `/caffeinate status` to show.
        config = self.config()
        system = FakeSystem()
        lib.write_state(self.state_file(config), 4242, NOW + 7200, CLAUDE_PID)
        rows = lib.held_sessions(config, system)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["live"], False)

    def test_non_json_files_in_the_state_directory_are_ignored(self):
        config = self.config()
        with open(os.path.join(self.tmp.name, "notes.txt"), "w") as handle:
            handle.write("hello")
        self.assertEqual(lib.held_sessions(config, FakeSystem()), [])

    def test_every_session_is_listed_not_just_the_first(self):
        config = self.config()
        system = FakeSystem()
        for session in ("s1", "s2", "s3"):
            lib.acquire(config, system, session, HOOK_PID)
        self.assertEqual(
            sorted(row["session"] for row in lib.held_sessions(config, system)),
            ["s1", "s2", "s3"])


class TestSweepStale(CaffeinateTestCase):
    """Records left by sessions that exited without firing SessionEnd.

    Every test here goes through `acquire` rather than calling `sweep_stale`
    directly, because the defect being fixed was in the WIRING — no caller ever
    cleaned another session's record — and a pure-function test would pass with
    the call deleted.
    """

    DEAD_PID = 4242

    def seed(self, config, session, expires_at, pid=DEAD_PID):
        lib.write_state(self.state_file(config, session), pid, expires_at, CLAUDE_PID)

    def test_an_expired_dead_record_from_another_session_is_swept(self):
        config = self.config()
        self.seed(config, "gone", NOW - 1)
        self.assertEqual(lib.acquire(config, FakeSystem(), "s1", HOOK_PID), "held")
        self.assertFalse(os.path.exists(self.state_file(config, "gone")))
        self.assertIsNotNone(self.recorded(config, "s1"))

    def test_a_dead_record_whose_deadline_is_still_ahead_is_kept(self):
        config = self.config()
        self.seed(config, "recent", NOW + 60)
        lib.acquire(config, FakeSystem(), "s1", HOOK_PID)
        self.assertTrue(os.path.exists(self.state_file(config, "recent")))

    def test_an_expired_record_whose_assertion_is_still_running_is_kept(self):
        # Deleting the only record of a live `caffeinate` orphans it: `status`
        # stops showing it and no hook will ever signal it.
        config = self.config()
        system = FakeSystem()
        system.table[self.DEAD_PID] = (1, "/usr/bin/caffeinate")
        self.seed(config, "live", NOW - 1)
        lib.acquire(config, system, "s1", HOOK_PID)
        self.assertTrue(os.path.exists(self.state_file(config, "live")))
        self.assertEqual(system.terminated, [])

    def test_the_battery_override_stamp_is_never_swept(self):
        # Same directory, same extension, and it is what makes the 24-hour
        # expiry work. Stamped far in the past so an age test would take it.
        config = self.config(require_ac=False)
        lib.write_stamp(lib.stamp_path(config), NOW - 1)
        lib.acquire(config, FakeSystem(), "s1", HOOK_PID)
        self.assertEqual(lib.read_stamp(lib.stamp_path(config)), NOW - 1)

    def test_files_that_are_not_records_are_left_alone(self):
        config = self.config()
        garbage = os.path.join(self.tmp.name, "hand-written.json")
        with open(garbage, "w") as handle:
            handle.write("not json")
        lib.acquire(config, FakeSystem(), "s1", HOOK_PID)
        self.assertTrue(os.path.exists(garbage))

    def test_a_failing_sweep_does_not_cost_the_hold(self):
        config = self.config()
        original = lib.sweep_stale

        def boom(*args, **kwargs):
            raise OSError("state directory vanished")

        lib.sweep_stale = boom
        self.addCleanup(setattr, lib, "sweep_stale", original)
        self.assertEqual(lib.acquire(config, FakeSystem(), "s1", HOOK_PID), "held")
        self.assertIsNotNone(self.recorded(config, "s1"))

    def test_a_record_renewed_mid_sweep_survives_the_sweep(self):
        # The Codex finding of 2026-09-27. The session owning an expired record
        # renews it AFTER the sweep has read it and BEFORE the unlink; without
        # the shared lock the renewed record is deleted and its live assertion
        # is orphaned. The renewal runs on a thread because it must genuinely
        # contend for the lock — in-line it would deadlock against it.
        #
        # join(0.5) is not a budget: with the lock the writer ALWAYS blocks and
        # the join always times out; only a broken lock lets it finish early,
        # and a slow machine can only make that mutation survive, never make a
        # correct build fail.
        import threading
        config = self.config()
        target = self.state_file(config, "renewing")
        self.seed(config, "renewing", NOW - 1)
        original = lib.read_state
        writers = []

        def interleave(path):
            record = original(path)
            if path == target and not writers:
                writer = threading.Thread(
                    target=lib.write_state, args=(target, 7777, NOW + 7200, CLAUDE_PID))
                writers.append(writer)
                writer.start()
                writer.join(0.5)
            return record

        lib.read_state = interleave
        self.addCleanup(setattr, lib, "read_state", original)
        lib.acquire(config, FakeSystem(), "s1", HOOK_PID)
        lib.read_state = original
        writers[0].join(5)

        self.assertFalse(writers[0].is_alive())
        self.assertEqual(self.recorded(config, "renewing")["caffeinate_pid"], 7777)

    def test_every_expired_record_goes_not_just_the_first(self):
        config = self.config()
        for index in range(5):
            self.seed(config, "gone%d" % index, NOW - 1 - index)
        lib.acquire(config, FakeSystem(), "s1", HOOK_PID)
        self.assertEqual(os.listdir(self.tmp.name), ["s1.json"])


# --- The wiring --------------------------------------------------------------

class TestTimeoutBudget(unittest.TestCase):
    """The declared hook timeout, and the subprocess timeouts it has to cover,
    are three numbers that move together. Stated in prose they drift: a comment
    describing exactly this relationship has gone wrong elsewhere in
    this repo. If someone adds a third blocking call, this is where it shows up.

    If the harness kills the hook mid-`ps` there is no verdict and no assertion,
    so the machine sleeps as it always did — the budget failing means losing the
    feature for one turn, never a wrong hold.
    """

    def settings_timeouts(self):
        found = {}
        for row in hook_manifest.registrations(hook_manifest.load(REPO)):
            if row["exe"] == "claude-caffeinate":
                found[row["event"]] = row["timeout"]
        return found

    def test_every_wired_mode_declares_the_budgeted_timeout(self):
        timeouts = self.settings_timeouts()
        self.assertEqual(
            sorted(timeouts),
            ["PostToolBatch", "SessionEnd", "Stop", "StopFailure", "UserPromptSubmit"])
        for event, declared in timeouts.items():
            self.assertEqual(declared, lib.HOOK_TIMEOUT, event)

    def test_the_declared_timeout_covers_every_subprocess_with_margin(self):
        # pmset, the process table, and the candidate shells' command lines.
        worst_case = 2 * lib.PS_TIMEOUT + lib.PMSET_TIMEOUT
        self.assertLess(worst_case, lib.HOOK_TIMEOUT)
        self.assertGreaterEqual(lib.HOOK_TIMEOUT - worst_case, 2)

    def test_the_wired_modes_are_the_ones_the_lib_implements(self):
        for event, mode in (("UserPromptSubmit", "acquire"), ("PostToolBatch", "acquire"),
                            ("Stop", "settle"), ("StopFailure", "settle"),
                            ("SessionEnd", "release")):
            rows = [r for r in hook_manifest.registrations(hook_manifest.load(REPO))
                    if r["event"] == event and r["exe"] == "claude-caffeinate"]
            self.assertEqual(len(rows), 1, event)
            self.assertEqual("tools/caffeinate/claude-caffeinate", rows[0]["relpath"])
            self.assertEqual(rows[0]["args"], [mode], rows[0]["command"])
            self.assertTrue(callable(getattr(lib, mode)))


class TestSubprocessTimeoutSeam(unittest.TestCase):
    """`_subprocess_timeout`: the real bound unless a test sandbox overrides it.

    Row 86. The override exists only for the executable tests below, whose
    shell-script shims overran the real 2-3 s bounds under local-ci load. It
    must be inert without `CLAUDE_TOOLKIT_CONFIG`, and only a finite value above
    zero may count: `subprocess.run(timeout=inf)` raises, which in `ps` would
    cost every hook its verdict.
    """

    # Where a test's config lives: inside the system temp directory, the
    # second condition the seam requires (the variable alone is also a real
    # runtime override).
    FIXTURE = os.path.join(tempfile.gettempdir(), "row86", "config.json")

    def bound(self, env):
        saved = dict(os.environ)
        try:
            for key in ("CLAUDE_TOOLKIT_CONFIG", "CAFFEINATE_TEST_SUBPROCESS_TIMEOUT"):
                os.environ.pop(key, None)
            os.environ.update(env)
            return lib._subprocess_timeout(lib.PS_TIMEOUT)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_no_override_is_the_real_bound(self):
        self.assertEqual(self.bound({"CLAUDE_TOOLKIT_CONFIG": self.FIXTURE}), lib.PS_TIMEOUT)

    def test_a_sandboxed_override_is_used(self):
        self.assertEqual(self.bound({"CLAUDE_TOOLKIT_CONFIG": self.FIXTURE,
                                     "CAFFEINATE_TEST_SUBPROCESS_TIMEOUT": "8"}), 8.0)

    def test_without_the_sandbox_signal_the_override_is_ignored(self):
        self.assertEqual(self.bound({"CAFFEINATE_TEST_SUBPROCESS_TIMEOUT": "8"}), lib.PS_TIMEOUT)

    def test_a_real_runtime_config_override_ignores_it(self):
        # Codex Code Review, row 86: CLAUDE_TOOLKIT_CONFIG is ALSO how a real
        # install points at its config. A live hook that took a longer bound
        # would overrun its 10 s budget and be killed without a verdict.
        real = os.path.expanduser("~/.config/claude-code-toolkit/config.json")
        for config in (real, "relative/config.json"):
            with self.subTest(config=config):
                self.assertEqual(
                    self.bound({"CLAUDE_TOOLKIT_CONFIG": config,
                                "CAFFEINATE_TEST_SUBPROCESS_TIMEOUT": "8"}),
                    lib.PS_TIMEOUT)

    def test_only_a_finite_value_above_zero_counts(self):
        for value in ("0", "-1", "inf", "nan", "eight", ""):
            with self.subTest(value=value):
                self.assertEqual(
                    self.bound({"CLAUDE_TOOLKIT_CONFIG": self.FIXTURE,
                                "CAFFEINATE_TEST_SUBPROCESS_TIMEOUT": value}),
                    lib.PS_TIMEOUT)


def shim_sleep_line(delay):
    """A shim's opening `sleep`, so a test can make it SLOW on purpose: the
    stand-in for a shim starved under local-ci's concurrent load (row 86)."""
    return "sleep %s\n" % delay if delay else ""


class TestHookExecutable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.config_path = os.path.join(self.tmp.name, "config.json")

    def write_config(self, **overrides):
        data = {"enabled": True, "require_ac": False,
                "hold_seconds": 2, "settle_seconds": 1, "state_dir": self.state}
        data.update(overrides)
        with open(self.config_path, "w") as handle:
            json.dump({"caffeinate": data}, handle)
        return self.config_path

    # Real `pmset -g ps` output, both shapes, kept as literals because that is
    # what the hook's parser is actually pointed at.
    AC_OUTPUT = ("Now drawing from 'AC Power'\n"
                 " -InternalBattery-0 (id=12345678)\t72%; charged; 0:00 "
                 "remaining present: true\n")
    BATTERY_OUTPUT = ("Now drawing from 'Battery Power'\n"
                      " -InternalBattery-0 (id=12345678)\t72%; discharging; "
                      "6:56 remaining present: true\n")

    def shim_pmset(self, output, delay=0):
        """Put a fake `pmset` on PATH so these tests DECLARE the power state.

        Without this the hook subprocess inherits `dict(os.environ)` and shells
        out to the real `pmset`, so the suite's answer depends on whether the
        machine running it is plugged in. That is not flakiness — it is a
        DIFFERENT CONTRACT PER MACHINE, and it shipped: three tests here assert
        an exact `state_files()` listing, the battery path writes
        `battery-override.json` into that same directory, and so they passed on
        AC and in CI while failing on any laptop running on battery.

        The hook calls `["pmset", "-g", "ps"]` by bare name, so PATH is the
        seam. Returns the directory to prepend.
        """
        shim = os.path.join(self.tmp.name, "shim")
        if not os.path.isdir(shim):
            os.makedirs(shim)
        path = os.path.join(shim, "pmset")
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + shim_sleep_line(delay)
                         + "cat <<'EOF'\n" + output + "EOF\n")
        os.chmod(path, 0o755)
        return shim

    def shim_ps(self, delay=0):
        """Put a `ps` on PATH that sees NO background jobs.

        Run from inside a Claude Code session, the hook's real ancestry reaches
        a real `claude`, and any `run_in_background` job that session has going
        is a real job — measured 2026-10-20, four tests here picked one up. So
        the command-line lookup the job scan depends on answers nothing, and
        every other `ps` call passes through, which the end-to-end tests need
        to see their own real `caffeinate`. TestPsShim is the positive control.
        """
        shim = os.path.join(self.tmp.name, "shim")
        os.makedirs(shim, exist_ok=True)
        path = os.path.join(shim, "ps")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\n' + shim_sleep_line(delay)
                         + 'case "$*" in *args=*) exit 0;; esac\n'
                         'exec /bin/ps "$@"\n')
        os.chmod(path, 0o755)
        return shim

    # What the hook's subprocess bound becomes under the test seam. Well above
    # any shim's real run time, and three of them still fit inside run_hook's
    # own 30 s timeout.
    SEAM_TIMEOUT = "8"

    def run_hook(self, mode, payload='{"session_id":"probe"}', config=None,
                 power=None, seam=True, pmset_delay=0, ps_delay=0):
        environment = dict(os.environ)
        environment["CAFFEINATE_DEBUG"] = "1"
        environment["CLAUDE_TOOLKIT_CONFIG"] = config or self.config_path
        # The shims are shell scripts, and under local-ci's concurrent suites
        # one overran the hook's 2-3 s bounds: `ps` raised (no verdict at all)
        # and `pmset` read as battery (a stray override stamp). Row 86 first
        # blamed the machine's power state; it was load. `seam=False` gives the
        # real bounds back, which is how the slow-shim tests prove the seam matters.
        if seam:
            environment["CAFFEINATE_TEST_SUBPROCESS_TIMEOUT"] = self.SEAM_TIMEOUT
        else:
            environment.pop("CAFFEINATE_TEST_SUBPROCESS_TIMEOUT", None)
        # Default AC, which is what every test here was written against and
        # what CI runners report. A test that cares about the battery branch
        # asks for it by name.
        self.shim_ps(ps_delay)
        shim = self.shim_pmset(self.AC_OUTPUT if power is None else power, pmset_delay)
        environment["PATH"] = shim + os.pathsep + environment.get("PATH", "")
        argv = [sys.executable, HOOK] + ([mode] if mode else [])
        return subprocess.run(argv, input=payload, capture_output=True,
                              text=True, env=environment, timeout=30)

    def state_files(self):
        try:
            return sorted(os.listdir(self.state))
        except OSError:
            return []

    def test_the_power_shim_actually_drives_the_hook(self):
        """The positive control for `shim_pmset`, and it is not optional.

        Defaulting every test to AC could have fixed the failures by making the
        suite BLIND to the battery path rather than hermetic about it — a
        strictly worse outcome than the defect it replaces, and one that looks
        identical from a green run. This asserts the two branches are both
        reachable and produce DIFFERENT observable state, on a machine whose
        real power source is whatever it happens to be.

        `battery-override.json` is the discriminator: the battery path stamps
        the override clock through `_refresh_stamp`, and the AC path never
        reaches it. That is exactly the file whose appearance broke three
        exact-listing assertions here.
        """
        self.write_config()
        self.run_hook("acquire", power=self.AC_OUTPUT)
        self.assertNotIn(lib.OVERRIDE_STAMP, self.state_files(),
                         "AC must not stamp the battery override clock")

        self.tearDownState()
        self.write_config()
        self.run_hook("acquire", power=self.BATTERY_OUTPUT)
        self.assertIn(lib.OVERRIDE_STAMP, self.state_files(),
                      "battery must stamp the override clock — if this fails "
                      "the shim is not reaching the hook and every other test "
                      "in this class is asserting against the real machine")

    def tearDownState(self):
        """Drop the state directory between the two halves above, so the second
        assertion cannot be satisfied by residue from the first."""
        shutil.rmtree(self.state, ignore_errors=True)

    def test_unknown_mode_does_nothing(self):
        # `returncode == 0` and an empty state dir are both also true of a hook
        # that crashed on line 1, since `except: pass` guarantees the exit code.
        # The paired run is what separates the two: same config, same input, a
        # mode the hook recognises, and it reports a verdict — so the file is
        # known to execute this far in exactly this configuration.
        self.write_config(enabled=False)
        rejected = self.run_hook("polish")
        self.assertEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stderr, "")
        self.assertEqual(self.state_files(), [])

        self.assertIn("disabled", self.run_hook("acquire").stderr)

    def test_no_mode_at_all_does_nothing(self):
        self.write_config(enabled=False)
        result = self.run_hook(None)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.state_files(), [])
        self.assertIn("disabled", self.run_hook("acquire").stderr)

    def test_disabled_config_holds_nothing(self):
        self.write_config(enabled=False)
        result = self.run_hook("acquire")
        self.assertEqual(result.returncode, 0)
        self.assertIn("disabled", result.stderr)
        self.assertEqual(self.state_files(), [])

    def test_absent_config_holds_nothing(self):
        # What a fresh clone looks like.
        result = self.run_hook("acquire", config="/nonexistent/config.json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.state_files(), [])

    @unittest.skipUnless(sys.platform == "darwin", "needs the real caffeinate")
    def test_a_payload_without_a_session_id_still_records_a_hold(self):
        # This replaces a test that asserted ONLY returncode == 0 against a hook
        # whose `except: pass` guarantees exit 0 — it would have passed if the
        # hook crashed on line 1. Garbage stdin means payload = {}, which is the
        # only coverage of safe_name(None), so the filename is the assertion.
        self.write_config()
        result = self.run_hook("acquire", payload="not json at all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state_files(), ["unknown.json"])

    @unittest.skipUnless(sys.platform == "darwin", "needs the real caffeinate")
    def test_stdout_stays_empty_because_acquire_runs_on_every_prompt(self):
        self.write_config()
        result = self.run_hook("acquire")
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.state_files(), ["probe.json"])

    @unittest.skipUnless(sys.platform == "darwin", "needs the real caffeinate")
    def test_acquire_settle_release_end_to_end(self):
        self.write_config()

        acquired = self.run_hook("acquire")
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        self.assertIn("held", acquired.stderr)
        self.assertEqual(self.state_files(), ["probe.json"])
        with open(os.path.join(self.state, "probe.json")) as handle:
            record = json.load(handle)
        self.assertGreater(record["caffeinate_pid"], 1)
        self.assertGreater(record["expires_at"], 0)

        # WHICH settle branch runs here is ambient, and asserting either one
        # would be asserting against the environment — the rule this repo
        # already broke once with a wall-clock budget. Run under Claude Code the
        # walk finds a live `claude` and the verdict is `session-alive`; run in
        # CI under a bare shell there is none, and it is `settling`. Both are
        # correct. What is invariant, and what this asserts, is that a hold
        # SURVIVES its settle either way — the regression worth catching is a
        # settle that drops the assertion outright.
        settled = self.run_hook("settle")
        self.assertEqual(settled.returncode, 0, settled.stderr)
        self.assertRegex(settled.stderr, r"session-alive|settling")
        self.assertEqual(self.state_files(), ["probe.json"])

        released = self.run_hook("release")
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(self.state_files(), [])

    # Row 86: just past each real bound (PMSET_TIMEOUT 3, PS_TIMEOUT 2), so
    # with the seam blanked the real hook fails exactly as it did under load.
    SLOW_PMSET = 3.5
    SLOW_PS = 2.5

    @unittest.skipUnless(sys.platform == "darwin", "needs the real caffeinate")
    def test_a_slow_pmset_is_still_read_as_the_declared_power(self):
        self.write_config()
        self.run_hook("acquire", pmset_delay=self.SLOW_PMSET, seam=False)
        self.assertIn(lib.OVERRIDE_STAMP, self.state_files(),
                      "control: a pmset slower than PMSET_TIMEOUT must read as "
                      "battery under the real bound, or this shim is not slow enough")
        self.run_hook("release", seam=False)

        self.tearDownState()
        self.write_config()
        self.run_hook("acquire", pmset_delay=self.SLOW_PMSET)
        self.assertEqual(self.state_files(), ["probe.json"])
        self.run_hook("release")

    @unittest.skipUnless(sys.platform == "darwin", "needs the real caffeinate")
    def test_a_slow_ps_still_reaches_a_verdict(self):
        self.write_config()
        starved = self.run_hook("acquire", ps_delay=self.SLOW_PS, seam=False)
        self.assertNotIn("held", starved.stderr,
                         "control: a ps slower than PS_TIMEOUT must cost the real "
                         "hook its verdict, or this shim is not slow enough")

        acquired = self.run_hook("acquire", ps_delay=self.SLOW_PS)
        self.assertIn("held", acquired.stderr)
        self.run_hook("release")

    @unittest.skipUnless(sys.platform == "darwin", "needs the real caffeinate")
    def test_a_real_acquire_sweeps_another_sessions_expired_record(self):
        # Through the executable, against the REAL process table: the seeded
        # pid is this test's own, which is alive and is not `caffeinate`.
        self.write_config()
        os.makedirs(self.state)
        lib.write_state(os.path.join(self.state, "gone.json"), os.getpid(), 1, None)

        acquired = self.run_hook("acquire")
        self.assertIn("held", acquired.stderr)
        self.assertEqual(self.state_files(), ["probe.json"])
        self.run_hook("release")

    @unittest.skipUnless(sys.platform == "darwin", "needs the real caffeinate")
    def test_the_recorded_bound_pid_really_is_a_claude_process(self):
        # The `session-alive` branch is only reachable if the walk actually finds
        # `claude` on this machine, and the walk failing is SILENT — the hold
        # stays bounded by `-t` and merely loses `-w`, so nothing visibly breaks
        # and every fake-table test still passes. Skipped rather than failed off
        # Claude Code, because there is genuinely no claude to find there.
        self.write_config()
        self.run_hook("acquire")
        with open(os.path.join(self.state, "probe.json")) as handle:
            bound = json.load(handle)["claude_pid"]
        if bound is None:
            self.skipTest("not running under a claude process")
        listing = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(bound)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(
            os.path.basename(listing.stdout.strip()), "claude", listing.stdout)


class TestCliModes(unittest.TestCase):
    """`/caffeinate` runs these. Unlike the hook modes they print, they take no
    stdin, and they are allowed to fail loudly — a toggle that silently does
    nothing is the one outcome it must never produce."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, exist_ok=True)

    def write_config(self, **overrides):
        data = {"enabled": True, "require_ac": True, "state_dir": self.state}
        data.update(overrides)
        with open(self.config_path, "w") as handle:
            json.dump({"owner_name": "Tester", "caffeinate": data}, handle, indent=2)

    def run_cli(self, *argv, config=None):
        environment = dict(os.environ)
        environment["CLAUDE_TOOLKIT_CONFIG"] = config or self.config_path
        # HOME is redirected so that state_dir's COMPILED default
        # (~/.claude/caffeinate) resolves inside the sandbox. That default is
        # reached whenever the config cannot be read, which is exactly what the
        # broken-config tests below arrange — and without this the tool writes
        # into the developer's real state directory while the test inspects the
        # temp one and passes. Measured: a mutation removing the `if ok:` guard
        # survived for precisely that reason, and left a stray
        # battery-override.json in the author's own ~/.claude/caffeinate.
        environment["HOME"] = self.home
        return subprocess.run([sys.executable, HOOK] + list(argv),
                              input="", capture_output=True, text=True,
                              env=environment, timeout=30)

    def default_stamp_path(self):
        """Where the tool writes when it cannot read the config at all."""
        return os.path.join(self.home, ".claude", "caffeinate",
                            lib.OVERRIDE_STAMP)

    def read_config(self):
        with open(self.config_path) as handle:
            return json.load(handle)["caffeinate"]

    def test_on_flips_the_config_and_says_so(self):
        self.write_config()
        result = self.run_cli("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(self.read_config()["require_ac"], False)
        self.assertIn("require_ac", result.stdout)

    def test_off_flips_it_back(self):
        self.write_config(require_ac=False)
        result = self.run_cli("off")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(self.read_config()["require_ac"], True)

    def test_off_leaves_the_feature_enabled_for_wall_power(self):
        self.write_config(require_ac=False)
        self.run_cli("off")
        self.assertIs(self.read_config()["enabled"], True)

    def test_running_on_twice_is_not_an_error(self):
        self.write_config()
        self.run_cli("on")
        repeat = self.run_cli("on")
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertIn("Already", repeat.stdout)

    def test_on_over_a_lapsed_override_says_it_restarted_not_already(self):
        # Measured 2026-09-27: `status` said LAPSED, then `on` printed "Already
        # holding on battery. No change" — true of config.json, false of the
        # behaviour, which it had just changed by restarting the clock.
        stamp = os.path.join(self.state, lib.OVERRIDE_STAMP)
        self.write_config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(stamp, int(time.time()) - 25 * 3600)

        result = self.run_cli("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LAPSED", result.stdout)
        self.assertIn("Restarted", result.stdout)
        self.assertNotIn("Already", result.stdout)
        self.assertIn("on battery  yes", self.run_cli("status").stdout)

        # The control: once active, a second `on` really is a no-op.
        repeat = self.run_cli("on")
        self.assertIn("Already", repeat.stdout)
        self.assertNotIn("LAPSED", repeat.stdout)

    def test_an_unreadable_config_fails_loudly_rather_than_silently(self):
        # Exit 0 with a reassuring message, on a toggle that wrote nothing, is
        # precisely the failure this whole feature is gated against.
        with open(self.config_path, "w") as handle:
            handle.write("{ not json")
        result = self.run_cli("on")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not read", result.stderr)

    def test_status_reports_the_config_without_changing_it(self):
        self.write_config()
        before = self.read_config()
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("caffeinate", result.stdout)
        self.assertIn("wall power only", result.stdout)
        self.assertEqual(self.read_config(), before)

    def test_status_names_the_battery_state_when_the_override_is_on(self):
        self.write_config(require_ac=False)
        out = self.run_cli("status").stdout
        self.assertIn("on battery  yes", out)

    def test_every_holding_state_says_how_to_turn_it_off(self):
        # RESTORED after a review found it missing. The pre-expiry status line
        # said `/caffeinate off`; the expiry rewrote that line into four states
        # and the hint survived in none of them — and the test that pinned it was
        # weakened in the same commit that dropped it. Asserting across all three
        # holding states, so adding a fourth cannot quietly reintroduce the gap.
        stamp = os.path.join(self.state, lib.OVERRIDE_STAMP)

        self.write_config(require_ac=False, battery_override_hours=24)
        lib.clear_stamp(stamp)
        self.assertIn("/caffeinate off", self.run_cli("status").stdout, "unstamped")

        lib.write_stamp(stamp, int(time.time()) - 3600)
        self.assertIn("/caffeinate off", self.run_cli("status").stdout, "active")

        self.write_config(require_ac=False, battery_override_hours=0)
        self.assertIn("/caffeinate off", self.run_cli("status").stdout, "forever")

    def test_a_lapsed_state_points_at_on_not_off(self):
        # The one holding-state exception, stated so the assertion above is not
        # widened to cover it by mistake: a lapsed override is not holding, so
        # the useful instruction is how to restart it.
        self.write_config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(os.path.join(self.state, lib.OVERRIDE_STAMP),
                        int(time.time()) - 25 * 3600)
        self.assertIn("/caffeinate on", self.run_cli("status").stdout)

    def test_status_says_a_lapsed_override_is_not_holding(self):
        # THE CASE THAT NEEDS WORDS. config.json still reads require_ac: false,
        # so the file and the behaviour genuinely disagree, and this output is
        # the only place that is visible. "no" alone would send someone to a
        # config file that appears to contradict it.
        self.write_config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(os.path.join(self.state, lib.OVERRIDE_STAMP),
                        int(time.time()) - 25 * 3600)
        out = self.run_cli("status").stdout
        self.assertIn("LAPSED", out)
        self.assertIn("/caffeinate on", out)

    def test_status_counts_down_an_active_override(self):
        # Stamped 90 minutes back, not 60, so the answer sits in the MIDDLE of
        # an hour rather than exactly on its boundary. At 60 the remaining time
        # is exactly 23h and one second of drift between the stamp and the read
        # flips `remaining // 3600` to 22 — which it did, intermittently, and
        # surfaced as a mutation harness reporting RESTORE FAILED on a suite
        # that was green when run by hand.
        self.write_config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(os.path.join(self.state, lib.OVERRIDE_STAMP),
                        int(time.time()) - 90 * 60)
        self.assertIn("lapses in 22h", self.run_cli("status").stdout)

    def test_status_says_when_the_override_never_lapses(self):
        self.write_config(require_ac=False, battery_override_hours=0)
        self.assertIn("never lapses", self.run_cli("status").stdout)

    def test_on_stamps_the_clock_and_says_when_it_lapses(self):
        self.write_config()
        result = self.run_cli("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LAPSES", result.stdout)
        self.assertIn("24h", result.stdout)
        stamped = lib.read_stamp(os.path.join(self.state, lib.OVERRIDE_STAMP))
        self.assertIsNotNone(stamped)
        self.assertLess(abs(stamped - int(time.time())), 60)

    def test_off_clears_the_clock(self):
        # Otherwise a later `on` would inherit the OLD stamp and could lapse
        # immediately, which reads as the command not working.
        self.write_config(require_ac=False)
        stamp = os.path.join(self.state, lib.OVERRIDE_STAMP)
        lib.write_stamp(stamp, int(time.time()))
        self.run_cli("off")
        self.assertIsNone(lib.read_stamp(stamp))

    def test_on_does_not_stamp_when_the_config_write_failed(self):
        # A machine whose config cannot be written must not believe the override
        # started — that would read as working, for 24 hours.
        #
        # BOTH paths are asserted, and that is the whole test. An unreadable
        # config means `state_dir` falls back to its compiled default, so the
        # stamp a broken build would write lands under $HOME, NOT in the
        # configured state directory. Checking only the latter is what let the
        # mutation removing the `if ok:` guard survive — and the surviving run
        # wrote a real file into the author's own ~/.claude/caffeinate.
        with open(self.config_path, "w") as handle:
            handle.write("{ not json")
        result = self.run_cli("on")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(
            lib.read_stamp(os.path.join(self.state, lib.OVERRIDE_STAMP)))
        self.assertFalse(os.path.exists(self.default_stamp_path()),
                         "stamped the fallback state dir on a failed write")

    def test_the_sandboxed_home_is_really_where_a_stamp_would_land(self):
        # POSITIVE CONTROL for the assertion above. If HOME were not redirected,
        # `default_stamp_path` would point at a directory the tool never touches
        # and the assertFalse would pass for free, forever.
        #
        # The config here is perfectly READABLE and merely omits `state_dir`,
        # which is what routes the stamp to the compiled default under $HOME. An
        # earlier version of this comment said "unreadable"; a review caught that
        # the stated mechanism was not the mechanism. The control still proves
        # what it needs to — only the explanation was wrong.
        with open(self.config_path, "w") as handle:
            json.dump({"caffeinate": {"enabled": True}}, handle)
        self.run_cli("on")
        self.assertTrue(os.path.exists(self.default_stamp_path()),
                        "HOME redirection is not reaching the tool")

    def test_on_reports_both_facts_when_the_clock_cannot_be_written(self):
        # THE BLOCKING DEFECT. The config write lands FIRST, so a failure to
        # write the clock cannot simply raise: that reports failure for a toggle
        # that took effect. Measured before the fix: a raw IsADirectoryError
        # traceback, exit 1, `require_ac: false` already on disk, and
        # `holds_on_battery` still True 365 days later.
        self.write_config()
        stamp = os.path.join(self.state, lib.OVERRIDE_STAMP)
        os.makedirs(stamp, exist_ok=True)   # unreplaceable: it is a directory

        result = self.run_cli("on")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("was written", result.stderr)      # the setting landed
        self.assertIn("could not be", result.stderr)     # the clock did not
        self.assertIn("stays OFF", result.stderr)        # and what that means


    def test_status_says_when_the_feature_is_off_entirely(self):
        self.write_config(enabled=False)
        self.assertIn("holds nothing at all", self.run_cli("status").stdout)

    def test_status_says_when_the_floor_is_disabled(self):
        self.write_config(battery_floor_percent=0)
        self.assertIn("run the battery flat", self.run_cli("status").stdout)

    def test_status_reports_no_holds_on_a_clean_machine(self):
        self.write_config()
        self.assertIn("nothing held right now", self.run_cli("status").stdout)

    def shim_pmset(self, output):
        """A `pmset` on PATH that prints `output`. Deterministic in both
        directions, where the real one only ever reports the state this machine
        happens to be in — and the branch that matters is the one you cannot
        reach while plugged in."""
        directory = os.path.join(self.tmp.name, "shim")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "pmset")
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\ncat <<'PMSET_EOF'\n%s\nPMSET_EOF\n" % output)
        os.chmod(path, 0o755)
        return directory

    def run_cli_on_battery(self, *argv, percent=12):
        shim = self.shim_pmset(
            "Now drawing from 'Battery Power'\n"
            " -InternalBattery-0 (id=1)\t%d%%; discharging; 1:00 remaining" % percent)
        environment = dict(os.environ)
        environment["CLAUDE_TOOLKIT_CONFIG"] = self.config_path
        environment["PATH"] = shim + os.pathsep + environment["PATH"]
        return subprocess.run([sys.executable, HOOK] + list(argv), input="",
                              capture_output=True, text=True, env=environment,
                              timeout=30)

    def test_on_warns_when_the_battery_is_already_under_the_floor(self):
        # `on` is the one path where the setting applies cleanly and still holds
        # nothing. A review found this warning UNREACHABLE in its first form: it
        # lived in the slash command's prose, conditioned on a power state that
        # `on` never read and never printed, so the model had no input from
        # which to notice it. Reaching it is now the command's own job, which is
        # why this asserts against the command's own output.
        self.write_config(battery_floor_percent=20, require_ac=True)
        result = self.run_cli_on_battery("on", percent=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BUT NOT YET", result.stdout)
        self.assertIn("12%", result.stdout)
        self.assertIn("20%", result.stdout)
        # The config write must still have happened — the warning says the hold
        # is deferred, not that the setting was refused.
        self.assertIs(self.read_config()["require_ac"], False)

    def test_on_does_not_warn_when_the_battery_is_above_the_floor(self):
        # The other direction, without which a version that ALWAYS warned would
        # pass the test above.
        self.write_config(battery_floor_percent=20, require_ac=True)
        result = self.run_cli_on_battery("on", percent=80)
        self.assertNotIn("BUT NOT YET", result.stdout)

    def test_on_does_not_warn_on_wall_power_at_any_charge(self):
        self.write_config(battery_floor_percent=20, require_ac=True)
        shim = self.shim_pmset(
            "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t5%; charging")
        environment = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=self.config_path)
        environment["PATH"] = shim + os.pathsep + environment["PATH"]
        result = subprocess.run([sys.executable, HOOK, "on"], input="",
                                capture_output=True, text=True,
                                env=environment, timeout=30)
        self.assertNotIn("BUT NOT YET", result.stdout)

    def test_status_warns_below_the_floor_while_the_override_is_live(self):
        # COVERS THE LINE A REVIEW FOUND UNTESTED. Reverting
        # `on_battery_now = holds_on_battery(...)` to `not require_ac(...)` left
        # all 169 tests green — the BELOW THE FLOOR branch had no test at all,
        # because the only status-on-battery test ran with require_ac=True and
        # never reached it.
        self.write_config(require_ac=False, battery_override_hours=0,
                          battery_floor_percent=20)
        out = self.run_cli_on_battery("status", percent=8).stdout
        self.assertIn("BELOW THE FLOOR", out)

    def test_status_does_not_warn_below_the_floor_once_the_override_lapsed(self):
        # The half that makes the line above load-bearing rather than incidental.
        # A lapsed override reads as `require_ac: false` in the file while
        # holding nothing, so the raw config value would still warn here — and
        # warning about a floor for a machine that is not holding anyway is
        # noise that trains someone to skip the whole notice.
        self.write_config(require_ac=False, battery_override_hours=24,
                          battery_floor_percent=20)
        lib.write_stamp(os.path.join(self.state, lib.OVERRIDE_STAMP),
                        int(time.time()) - 25 * 3600)
        out = self.run_cli_on_battery("status", percent=8).stdout
        self.assertNotIn("BELOW THE FLOOR", out)
        self.assertIn("LAPSED", out)

    def test_off_does_not_warn_about_the_floor(self):
        # `off` stops holding on battery entirely, so the floor is irrelevant to
        # it. Pinning this stops the warning being hoisted somewhere it reads as
        # a reason `off` did not take effect.
        self.write_config(battery_floor_percent=20, require_ac=False)
        self.assertNotIn("BUT NOT YET", self.run_cli_on_battery("off").stdout)

    def test_the_shim_is_really_being_used(self):
        # POSITIVE CONTROL for the three tests above. If the shim were not on
        # PATH they would all read the real `pmset`, and on a plugged-in machine
        # every one of them would pass while proving nothing.
        self.write_config()
        self.assertIn("battery, 12%", self.run_cli_on_battery("status").stdout)

    def run_cli_without_pmset(self, *argv):
        """Run with a PATH containing nothing at all, so `pmset` cannot resolve.

        This is what CI is: `pmset` is a macOS binary and the runners are Linux.
        Reproduced deliberately rather than skipped, because the bug it exposed
        is not a CI artifact — a hung or broken `pmset` on a real Mac takes the
        same branch.
        """
        empty = os.path.join(self.tmp.name, "empty-path")
        os.makedirs(empty, exist_ok=True)
        environment = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=self.config_path)
        environment["PATH"] = empty
        return subprocess.run([sys.executable, HOOK] + list(argv), input="",
                              capture_output=True, text=True, env=environment,
                              timeout=30)

    def test_on_still_succeeds_when_the_power_state_cannot_be_read(self):
        # THE ONE THAT MATTERS. `on` writes the config and THEN reads the power
        # state, only to decide whether to print a warning. An exception there
        # exited 1 after the write had already landed — telling a human the
        # toggle failed when it had succeeded, which is the worst shape a toggle
        # can fail in. Caught by CI, never by the author's Mac.
        self.write_config()
        result = self.run_cli_without_pmset("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(self.read_config()["require_ac"], False)

    def test_status_still_reports_when_the_power_state_cannot_be_read(self):
        self.write_config()
        result = self.run_cli_without_pmset("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unknown charge", result.stdout)

    def test_an_unreadable_power_state_never_claims_wall_power(self):
        # The safe direction, asserted rather than assumed: wall power is the
        # state that holds MORE, so a machine that cannot answer must not be
        # mistaken for one.
        self.write_config()
        self.assertNotIn("wall power,", self.run_cli_without_pmset("status").stdout)

    def test_an_unknown_mode_is_silent_when_stdin_is_a_pipe(self):
        # The hook contract. These are wired on UserPromptSubmit, so a mis-wired
        # mode exiting non-zero could block a prompt.
        self.write_config()
        result = self.run_cli("polish")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")

    def test_an_unknown_mode_from_a_terminal_says_so(self):
        # The other half, and it needs a real pty — `isatty` is the whole
        # discriminator, so a test that pipes stdin proves only the branch
        # above. Without this the terminal path would be uncovered, and an
        # uncovered path has no mutation to survive.
        self.write_config()
        primary, secondary = pty.openpty()
        try:
            result = subprocess.run(
                [sys.executable, HOOK, "polish"],
                stdin=secondary, capture_output=True, text=True, timeout=30,
                env=dict(os.environ, CLAUDE_TOOLKIT_CONFIG=self.config_path))
        finally:
            os.close(primary)
            os.close(secondary)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown mode", result.stderr)

    def test_the_two_mode_sets_do_not_overlap(self):
        # Collapsing them would let `status` be wired as a hook, printing to
        # stdout on every prompt submit — into the model's context, forever.
        source = open(HOOK).read()
        namespace = {}
        for line in source.splitlines():
            if line.startswith(("HOOK_MODES", "CLI_MODES")):
                exec(line, namespace)
        self.assertEqual(
            set(namespace["HOOK_MODES"]) & set(namespace["CLI_MODES"]), set())
        for mode in namespace["HOOK_MODES"]:
            self.assertTrue(callable(getattr(lib, mode)), mode)


class TestClockFailureDeclinesToHold(CaffeinateTestCase):
    """An expiry that cannot be recorded is not an expiry.

    `override_state` reads an unwritable stamp as `unstamped`, the LENIENT
    branch, so without this the override re-grants a fresh window on every call
    and holds forever — the exact failure the expiry exists to prevent, arrived
    at through the mechanism meant to prevent it."""

    def unwritable_stamp(self, config):
        path = lib.stamp_path(config)
        os.makedirs(path, exist_ok=True)

    def test_acquire_declines_when_the_clock_cannot_be_written(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        self.unwritable_stamp(config)
        system = FakeSystem(on_ac=False, percent=80)
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "on-battery-noclock")
        self.assertEqual(system.spawned, [])

    def test_it_releases_a_hold_taken_while_the_clock_still_worked(self):
        config = self.config(require_ac=False, battery_override_hours=24)
        system = FakeSystem(on_ac=False, percent=80)
        lib.acquire(config, system, "s1", HOOK_PID)
        held = self.recorded(config)["caffeinate_pid"]

        lib.clear_stamp(lib.stamp_path(config))
        self.unwritable_stamp(config)
        system.advance(STAMP_THROTTLE + 1)
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "released-noclock")
        self.assertEqual(system.terminated, [held])
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_wall_power_is_unaffected_by_an_unwritable_clock(self):
        # The clock only matters to the battery override. A machine at a desk
        # must not stop holding because a stamp path is broken.
        config = self.config(require_ac=False, battery_override_hours=24)
        self.unwritable_stamp(config)
        self.assertEqual(
            lib.acquire(config, FakeSystem(on_ac=True), "s1", HOOK_PID), "held")

    def test_no_expiry_configured_means_no_clock_to_fail(self):
        config = self.config(require_ac=False, battery_override_hours=0)
        self.unwritable_stamp(config)
        self.assertEqual(
            lib.acquire(config, FakeSystem(on_ac=False, percent=80),
                        "s1", HOOK_PID), "held")

    def test_the_throttle_does_not_mask_a_broken_clock_forever(self):
        # A fresh stamp lets the throttle skip the write, which is correct — but
        # once it lapses past the throttle the failure must surface rather than
        # being skipped indefinitely.
        config = self.config(require_ac=False, battery_override_hours=24)
        system = FakeSystem(on_ac=False, percent=80)
        lib.acquire(config, system, "s1", HOOK_PID)
        system.advance(60)
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "already-held")

        lib.clear_stamp(lib.stamp_path(config))
        self.unwritable_stamp(config)
        system.advance(STAMP_THROTTLE + 1)
        self.assertEqual(
            lib.acquire(config, system, "s1", HOOK_PID), "released-noclock")

    def test_refresh_reports_success_when_there_is_nothing_to_write(self):
        # Both no-op paths must report TRUE, or an ordinary machine would stop
        # holding on battery the moment the throttle skipped a write.
        forever = self.config(require_ac=False, battery_override_hours=0)
        self.assertIs(lib._refresh_stamp(forever, NOW), True)

        throttled = self.config(require_ac=False, battery_override_hours=24)
        lib.write_stamp(lib.stamp_path(throttled), NOW)
        self.assertIs(lib._refresh_stamp(throttled, NOW + 1), True)


# --- Ownership is one transaction --------------------------------------------

class GatedSystem(FakeSystem):
    """Pauses the FIRST spawn, so a test can hold `acquire` between spawning the
    process and recording it — the window in which nothing yet owns the hold."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.a_spawning = threading.Event()
        self.resume_a = threading.Event()
        self._gated = False

    def spawn(self, argv):
        pid = super().spawn(argv)
        if not self._gated:
            self._gated = True
            self.a_spawning.set()
            if not self.resume_a.wait(10):
                raise RuntimeError("test never released the paused spawn")
        return pid


class TestOwnershipIsOneTransaction(CaffeinateTestCase):
    def start(self, name, target, results):
        def run():
            results[name] = target()
        thread = threading.Thread(target=run, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def setUp(self):
        super().setUp()
        self.threads = []
        # Registered last-in-first-out AFTER the temp dir's cleanup, so every
        # thread has stopped writing before the directory is removed.
        self.addCleanup(self.stop_threads)

    def stop_threads(self):
        gate = getattr(self, "gate", None)
        if gate is not None:
            gate.set()
        for thread in self.threads:
            thread.join(10)

    def pause_acquire(self):
        """A = `acquire` paused after spawning. Returns (config, system, results,
        thread A); the release of the pause is registered as a cleanup so a
        failing assertion cannot strand the thread."""
        config = self.config()
        system = GatedSystem()
        self.gate = system.resume_a
        results = {}
        thread_a = self.start(
            "A", lambda: lib.acquire(config, system, "s1", HOOK_PID), results)
        self.assertTrue(system.a_spawning.wait(10), "A never reached spawn")
        return config, system, results, thread_a

    def finish(self, system, *threads):
        system.resume_a.set()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive(), "a thread never finished")

    def assert_blocked(self, thread):
        thread.join(0.3)
        self.assertTrue(thread.is_alive(), "B should be waiting on the lock")

    def test_concurrent_acquires_spawn_once(self):
        config, system, results, thread_a = self.pause_acquire()
        thread_b = self.start(
            "B", lambda: lib.acquire(config, system, "s1", HOOK_PID), results)
        self.assert_blocked(thread_b)
        self.finish(system, thread_a, thread_b)

        self.assertEqual(sorted(results.values()), ["already-held", "held"])
        self.assertEqual(len(system.spawned), 1)
        self.assertEqual(
            self.recorded(config)["caffeinate_pid"], 9000)

    def test_release_during_acquire_leaves_no_untracked_hold(self):
        config, system, results, thread_a = self.pause_acquire()
        thread_b = self.start(
            "B", lambda: lib.release(config, system, "s1"), results)
        self.assert_blocked(thread_b)
        self.finish(system, thread_a, thread_b)

        self.assertEqual(results["B"], "released")
        self.assertIn(9000, system.terminated)
        self.assertFalse(os.path.exists(self.state_file(config)))

    def test_settle_during_acquire_waits_for_the_record(self):
        config, system, results, thread_a = self.pause_acquire()
        thread_b = self.start(
            "B", lambda: lib.settle(config, system, "s1", HOOK_PID), results)
        self.assert_blocked(thread_b)
        self.finish(system, thread_a, thread_b)

        self.assertEqual(results["B"], "session-alive")

    def test_write_state_creates_a_missing_directory(self):
        path = os.path.join(self.tmp.name, "not", "yet", "s1.json")
        lib.write_state(path, 4242, NOW + 60, CLAUDE_PID)
        self.assertEqual(
            lib.read_state(path),
            {"caffeinate_pid": 4242, "expires_at": NOW + 60,
             "claude_pid": CLAUDE_PID})

    def test_fast_path_takes_no_lock(self):
        config = self.config()
        system = FakeSystem()
        self.assertEqual(lib.acquire(config, system, "s1", HOOK_PID), "held")

        results = {}
        with lib.records_lock(self.tmp.name):
            worker = self.start(
                "W", lambda: lib.acquire(config, system, "s1", HOOK_PID), results)
            worker.join(1)
            finished = not worker.is_alive()
        worker.join(10)
        self.assertTrue(finished, "the fast path waited on the records lock")
        self.assertEqual(results["W"], "already-held")


if __name__ == "__main__":
    unittest.main(verbosity=2)
