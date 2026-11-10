#!/usr/bin/env python3
"""Drive both scripts as subprocesses against a synthetic transcript.

The repo standard: test the executables, not just the lib. A sibling tool
shipped 93 green unit tests with zero lines of any executable exercised, and a
review reintroduced two already-fixed bugs without turning the suite red.

Every test here builds its own transcript directory and its own settings tree,
and sandboxes $HOME, so nothing asserts against this machine's real history or
real config.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def tool_use(name, **inp):
    return {"timestamp": "2026-08-29T00:00:00Z", "message": {"content": [
        {"type": "tool_use", "name": name, "input": inp}]}}


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.project = os.path.join(self.root, "project")
        self.ws = os.path.join(self.root, "ws")
        os.makedirs(self.project)
        os.makedirs(os.path.join(self.ws, ".claude"))
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = os.path.join(self.root, "home")
        os.makedirs(os.path.join(os.environ["HOME"], ".claude"))

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        self.tmp.cleanup()

    def write_transcript(self, records, name="session.jsonl"):
        with open(os.path.join(self.project, name), "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def write_settings(self, allow=(), deny=(), ask=(), scope="project"):
        path = {"project": os.path.join(self.ws, ".claude/settings.json"),
                "local": os.path.join(self.ws, ".claude/settings.local.json"),
                "user": os.path.join(os.environ["HOME"],
                                     ".claude/settings.json")}[scope]
        with open(path, "w") as fh:
            json.dump({"permissions": {"allow": list(allow),
                                       "deny": list(deny),
                                       "ask": list(ask)}}, fh)
        return path

    def run_script(self, script, *extra):
        # stdin=DEVNULL and an explicit timeout: a suite can pass from a pipe
        # and hang from a terminal, and capture_output does not redirect stdin.
        env = dict(os.environ)
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, script),
             "--project", self.project, "--workspace", self.ws, *extra],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=120, env=env)


class TestScannerSeesMcp(ScriptCase):
    def test_an_uncovered_mcp_server_is_reported_with_a_proposed_rule(self):
        self.write_transcript([tool_use("mcp__acme__get_thing")] * 3
                              + [tool_use("Bash", command="ls")])
        self.write_settings()
        r = self.run_script("scan_transcripts.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mcp__acme", r.stdout)
        self.assertIn("1 of 1 tools uncovered", r.stdout)

    def test_a_server_wide_rule_removes_the_proposal(self):
        self.write_transcript([tool_use("mcp__acme__get_thing")] * 3
                              + [tool_use("Bash", command="ls")])
        self.write_settings(allow=["mcp__acme"])
        r = self.run_script("scan_transcripts.py")
        self.assertIn("server-wide rule present", r.stdout)
        self.assertNotIn("proposed:", r.stdout)

    def test_a_destructive_tool_is_named_before_a_whole_server_grant(self):
        self.write_transcript([tool_use("mcp__acme__delete_thing"),
                               tool_use("Bash", command="ls")])
        self.write_settings()
        r = self.run_script("scan_transcripts.py")
        self.assertIn("mcp__acme__delete_thing", r.stdout)
        self.assertIn("outward-facing or destructive", r.stdout)

    def test_the_json_mode_carries_the_mcp_findings(self):
        self.write_transcript([tool_use("mcp__acme__get_thing"),
                               tool_use("Bash", command="ls")])
        self.write_settings()
        r = self.run_script("scan_transcripts.py", "--json")
        data = json.loads(r.stdout)
        self.assertEqual(data["mcp_servers"]["acme"]["proposed_rule"],
                         "mcp__acme")
        self.assertEqual(data["mcp_servers"]["acme"]["prompting"],
                         ["mcp__acme__get_thing"])

    def test_settings_scopes_merge_when_the_scanner_runs(self):
        # The scanner used to read one file. A grant in the LOCAL scope must
        # count, or every workspace-approved tool reads as uncovered.
        self.write_transcript([tool_use("mcp__acme__get_thing"),
                               tool_use("Bash", command="ls")])
        self.write_settings(allow=["mcp__acme"], scope="local")
        r = self.run_script("scan_transcripts.py")
        self.assertIn("server-wide rule present", r.stdout)


class TestScannerFindsTheConfiguredWorkspace(ScriptCase):
    def run_without_workspace_flag(self):
        # No --workspace: the script must find the repo's lib/ from its own
        # path and read workspace_root from the config, or the settings scopes
        # under it are never merged.
        cfg = os.path.join(self.root, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"workspace_root": self.ws}, fh)
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=cfg)
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "scan_transcripts.py"),
             "--project", self.project],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=120, env=env)

    def test_the_configured_workspace_settings_are_read(self):
        self.write_transcript([tool_use("mcp__acme__get_thing"),
                               tool_use("Bash", command="ls")])
        self.write_settings(allow=["mcp__acme"])
        r = self.run_without_workspace_flag()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("server-wide rule present", r.stdout)

    def test_control_without_the_grant_the_server_is_proposed(self):
        # Positive control: the line above comes from the grant, not from
        # the server being ignored.
        self.write_transcript([tool_use("mcp__acme__get_thing"),
                               tool_use("Bash", command="ls")])
        self.write_settings()
        r = self.run_without_workspace_flag()
        self.assertNotIn("server-wide rule present", r.stdout)
        self.assertIn("proposed:", r.stdout)


class TestReplayCountsNonBash(ScriptCase):
    def transcript_with_uncovered_mcp(self):
        self.write_transcript([tool_use("Bash", command="ls")] * 5
                              + [tool_use("mcp__acme__get_thing")] * 5)

    def test_an_uncovered_mcp_call_is_charged_to_the_gate_number(self):
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("mcp: acme", r.stdout)
        self.assertNotIn("Would prompt: 0/", r.stdout)

    def test_bash_only_mode_reproduces_the_old_blind_spot(self):
        # The regression this change exists to prevent: Bash-only scoring is
        # clean while every MCP call prompts. Asserting it keeps the contrast
        # honest rather than letting the new default quietly bury it.
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)"])
        r = self.run_script("replay_permissions.py", "--bash-only")
        self.assertIn("Would prompt: 0/5", r.stdout)
        self.assertNotIn("mcp: acme", r.stdout)

    def test_a_server_wide_grant_clears_the_mcp_residue(self):
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)", "mcp__acme"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 0/10", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_a_deny_rule_still_counts_as_not_prompting(self):
        # A denied call does not interrupt the user; it fails. It must not be
        # charged to the prompt number, or a correct carve-out looks like a
        # regression and gets removed.
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)", "mcp__acme"],
                            deny=["mcp__acme__get_thing"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 0/10", r.stdout)

    def test_an_unverified_builtin_is_excluded_not_guessed(self):
        self.write_transcript([tool_use("Bash", command="ls")] * 5
                              + [tool_use("SomeFutureTool")] * 4)
        self.write_settings(allow=["Bash(*)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("NOT MODELLED", r.stdout)
        self.assertIn("SomeFutureTool", r.stdout)
        # Excluded from the gate number, in BOTH the numerator and denominator.
        self.assertIn("Would prompt: 0/5", r.stdout)

    def test_an_explicit_settings_file_does_not_borrow_merged_scopes(self):
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)", "mcp__acme"], scope="local")
        lone = os.path.join(self.root, "proposed.json")
        with open(lone, "w") as fh:
            json.dump({"permissions": {"allow": ["Bash(*)"]}}, fh)
        r = self.run_script("replay_permissions.py", "--settings", lone)
        # The local scope's mcp__acme must NOT rescue this proposal.
        self.assertIn("mcp: acme", r.stdout)

    def test_an_ask_rule_is_reported_as_a_prompt_by_design(self):
        # An `ask` rule interrupts. Reporting it as no-prompt is how a config
        # with carve-outs on hot tools scores a serene 0% and still nags.
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)", "mcp__acme"],
                            ask=["mcp__acme__get_thing"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("PROMPTS BY DESIGN", r.stdout)
        self.assertIn("mcp__acme__get_thing", r.stdout)

    def test_an_ask_rule_is_not_charged_to_the_gate_number(self):
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)", "mcp__acme"],
                            ask=["mcp__acme__get_thing"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 0/5", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_a_deny_rule_is_not_reported_as_a_prompt_by_design(self):
        # Positive control for the bucket above: deny and ask must not collapse
        # into one another, or the carve-out advice becomes unreadable.
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)", "mcp__acme"],
                            deny=["mcp__acme__get_thing"])
        r = self.run_script("replay_permissions.py")
        self.assertNotIn("PROMPTS BY DESIGN", r.stdout)

    def test_the_threshold_still_gates_and_exits_non_zero(self):
        self.transcript_with_uncovered_mcp()
        self.write_settings(allow=["Bash(*)"])
        # Both directions. Asserting only the failing side would pass
        # identically if --threshold were ignored entirely, since this fixture
        # already fails at the 5.0 default.
        self.assertEqual(self.run_script(
            "replay_permissions.py", "--threshold", "1").returncode, 1)
        self.assertEqual(self.run_script(
            "replay_permissions.py", "--threshold", "90").returncode, 0)


class TestBashAskRules(ScriptCase):
    """Bash `ask` rules are the surface this repo's own baseline set uses."""

    def bash_transcript(self):
        self.write_transcript([tool_use("Bash", command="git status")] * 3
                              + [tool_use("Bash", command="git push origin main")]
                              + [tool_use("Bash", command="gh pr merge 12")])

    def test_a_bash_ask_rule_is_reported_as_a_prompt_by_design(self):
        # Before the fix: `Bash(*)` allow + these ask rules reported 0% with no
        # PROMPTS BY DESIGN section, on the exact rules the baseline ships.
        self.bash_transcript()
        self.write_settings(allow=["Bash(*)"],
                            ask=["Bash(git push *)", "Bash(gh pr merge *)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("PROMPTS BY DESIGN", r.stdout)
        self.assertIn("git push", r.stdout)
        self.assertIn("gh pr merge", r.stdout)

    def test_a_bash_ask_rule_is_not_charged_to_the_gate_number(self):
        self.bash_transcript()
        self.write_settings(allow=["Bash(*)"],
                            ask=["Bash(git push *)", "Bash(gh pr merge *)"])
        r = self.run_script("replay_permissions.py")
        # 3 replayed, 2 diverted to by-design; the ask'd calls leave the total.
        self.assertIn("Would prompt: 0/3", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_a_bash_command_with_no_ask_rule_is_untouched(self):
        # Positive control: without ask rules the same fixture must NOT print
        # the section, or the assertion above passes on unconditional output.
        self.bash_transcript()
        self.write_settings(allow=["Bash(*)"])
        r = self.run_script("replay_permissions.py")
        self.assertNotIn("PROMPTS BY DESIGN", r.stdout)
        self.assertIn("Would prompt: 0/5", r.stdout)


class TestKnownPromptingTools(ScriptCase):
    def test_an_uncovered_edit_is_charged_not_excused(self):
        # `is_modelled` used to be "is it MCP", so an uncovered Edit/Write/
        # WebFetch was struck from the gate number on a baseline-shaped config.
        self.write_transcript([tool_use("Bash", command="ls")] * 2
                              + [tool_use("Edit", file_path="/x")] * 3
                              + [tool_use("WebFetch", url="https://x")] * 2)
        self.write_settings(allow=["Bash(*)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 5/7", r.stdout)
        self.assertNotIn("Edit", r.stdout.split("NOT MODELLED")[-1]
                         if "NOT MODELLED" in r.stdout else "")

    def test_an_allowed_edit_is_not_charged(self):
        self.write_transcript([tool_use("Bash", command="ls")] * 2
                              + [tool_use("Edit", file_path="/x")] * 3)
        self.write_settings(allow=["Bash(*)", "Edit"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 0/5", r.stdout)

    def test_a_genuinely_unknown_tool_is_still_excluded(self):
        # The narrowing must not swallow the case it was built for.
        self.write_transcript([tool_use("Bash", command="ls")] * 2
                              + [tool_use("SomeFutureTool")] * 3)
        self.write_settings(allow=["Bash(*)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("NOT MODELLED", r.stdout)
        self.assertIn("Would prompt: 0/2", r.stdout)


class TestSkillPrompts(ScriptCase):
    """`Skill` sat in ALWAYS_FREE_TOOLS, so an uncovered /build call was
    counted as free and no audit ever proposed the rule that stops it."""

    def skill_transcript(self):
        self.write_transcript([tool_use("Bash", command="ls")] * 2
                              + [tool_use("Skill", skill="build")] * 3)

    def test_an_uncovered_skill_is_charged_not_excused(self):
        self.skill_transcript()
        self.write_settings(allow=["Bash(*)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 3/5", r.stdout)

    def test_an_allowed_skill_is_not_charged(self):
        # Positive control: the 3/5 above must come from the missing rule.
        self.skill_transcript()
        self.write_settings(allow=["Bash(*)", "Skill"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 0/5", r.stdout)

    def test_the_autonomous_grant_covers_skills(self):
        # Pins the shipped file: dropping "Skill" from it re-opens the prompt.
        repo = os.path.dirname(os.path.dirname(SCRIPTS))
        repo = os.path.dirname(os.path.dirname(repo))
        with open(os.path.join(repo, "claude/permissions.autonomous.json")) as fh:
            allow = json.load(fh)["permissions"]["allow"]
        self.skill_transcript()
        self.write_settings(allow=allow)
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 0/5", r.stdout)


class TestEverySubcommandMustMatch(ScriptCase):
    """`&` and `|&` are separators: a rule for the left side must not cover the
    command after them."""

    def replay(self, command, allow):
        self.write_transcript([tool_use("Bash", command=command)])
        self.write_settings(allow=allow)
        return self.run_script("replay_permissions.py")

    def test_a_background_ampersand_splits(self):
        r = self.replay("echo ok & python3 d.py", ["Bash(echo *)"])
        self.assertIn("Would prompt: 1/1", r.stdout)
        self.assertIn("python3", r.stdout)

    def test_a_pipe_ampersand_splits(self):
        r = self.replay("echo ok |& python3 d.py", ["Bash(echo *)"])
        self.assertIn("Would prompt: 1/1", r.stdout)
        self.assertIn("python3", r.stdout)

    def test_control_both_sides_granted_is_clean(self):
        r = self.replay("echo ok & python3 d.py", ["Bash(echo *)", "Bash(python3 *)"])
        self.assertIn("Would prompt: 0/1", r.stdout)

    def test_a_redirect_ampersand_does_not_split(self):
        r = self.replay("make 2>&1", ["Bash(make *)"])
        self.assertIn("Would prompt: 0/1", r.stdout)


class TestAllowRulesSkipOnlySafeAssignments(ScriptCase):
    def replay(self, command):
        self.write_transcript([tool_use("Bash", command=command)])
        self.write_settings(allow=["Bash(npm test)"])
        return self.run_script("replay_permissions.py")

    def test_an_unsafe_assignment_is_not_covered(self):
        self.assertIn("Would prompt: 1/1", self.replay("FOO=bar npm test").stdout)

    def test_a_safe_assignment_is_covered(self):
        self.assertIn("Would prompt: 0/1", self.replay("NODE_ENV=test npm test").stdout)

    def test_a_deny_rule_still_matches_past_any_assignment(self):
        self.write_transcript([tool_use("Bash", command="FOO=bar rm -rf tmp")])
        self.write_settings(allow=["Bash(*)"], deny=["Bash(rm *)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("DENIED", r.stdout)
        self.assertIn("rm -rf tmp", r.stdout)


class TestBashDenyRules(ScriptCase):
    def test_a_denied_call_is_blocked_not_prompting(self):
        self.write_transcript([tool_use("Bash", command="python3 d.py")])
        self.write_settings(deny=["Bash(python3 *)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 0/1", r.stdout)
        self.assertIn("DENIED", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_deny_outranks_a_blanket_allow(self):
        self.write_transcript([tool_use("Bash", command="rm -rf x")])
        self.write_settings(allow=["Bash(*)"], deny=["Bash(rm *)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("DENIED", r.stdout)
        self.assertIn("Bash: rm -rf x", r.stdout)

    def test_deny_outranks_ask(self):
        self.write_transcript([tool_use("Bash", command="rm -rf x")])
        self.write_settings(ask=["Bash(rm *)"], deny=["Bash(rm *)"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("DENIED", r.stdout)
        self.assertNotIn("PROMPTS BY DESIGN", r.stdout)

    def test_control_no_deny_rule_no_denied_section(self):
        self.write_transcript([tool_use("Bash", command="python3 d.py")])
        self.write_settings()
        r = self.run_script("replay_permissions.py")
        self.assertNotIn("DENIED", r.stdout)
        self.assertIn("Would prompt: 1/1", r.stdout)


class TestMcpOnlyHistory(ScriptCase):
    def test_an_mcp_only_history_is_replayed(self):
        self.write_transcript([tool_use("mcp__acme__get_thing")])
        self.write_settings()
        r = self.run_script("replay_permissions.py")
        self.assertIn("mcp: acme", r.stdout)
        self.assertNotIn("No ", r.stderr)

    def test_a_granted_mcp_only_history_passes(self):
        self.write_transcript([tool_use("mcp__acme__get_thing")])
        self.write_settings(allow=["mcp__acme"])
        r = self.run_script("replay_permissions.py")
        self.assertIn("Would prompt: 0/1", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_bash_only_mode_still_refuses_an_empty_bash_history(self):
        self.write_transcript([tool_use("mcp__acme__get_thing")])
        self.write_settings()
        r = self.run_script("replay_permissions.py", "--bash-only")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No Bash calls", r.stderr)

    def test_an_empty_history_still_refuses(self):
        self.write_transcript([])
        self.write_settings()
        r = self.run_script("replay_permissions.py")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No tool calls", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
