#!/usr/bin/env python3
"""Behavioural tests for `run_probes`, the default mode (no flag),
and `--status`.

No real `claude` binary is ever invoked here -- a stub `claude` script
stands in for it, written once per test to a throwaway `bin/` directory
placed first on `PATH` (`CLAUDE_CODE_EXECPATH` is never set, so binary
resolution always falls through to that `PATH` entry). The stub emulates
just enough of `claude -p --output-format stream-json` to drive `evaluate`:
it writes a transcript under `HARNESS_CHECK_PROJECTS_ROOT`, prints an
init/tool_use/result stream to stdout, and honours a `STUB_MODE` switch
(`healthy`, `opus`, `leak-topic`, `exit1`, `scoped-r2-only`,
`foreign-scratch`, and two that act only on the third run, whose
`autoMemoryDirectory` is `<cwd>/mem-lines`: `uncut-lines` -- the 200-line cut
never happens -- and `r3-exit1`). It also touches a marker file on every invocation,
purely so a test can assert it was never run at all.

It also emulates the scratchpad directory a real child `claude -p` leaves
behind (the leak `_cleanup_scratchpad` in `harness_lib.py` fixes): unless in
`foreign-scratch` mode, it creates
`<HARNESS_CHECK_STUB_TMP>/<project_slug(cwd)>/<session_id>/scratchpad` and
reports it as `init.scratchpad_path`, exactly the shape `run_probes`'
cleanup keys on. `foreign-scratch` instead reports a pre-existing directory
named by `HARNESS_CHECK_STUB_FOREIGN_SCRATCH`, whose slug component does NOT
match the fixture's own -- the guard case cleanup must refuse to touch.

The stub does not merely emulate -- it ENFORCES. It validates its own argv
against every flag `_probe_argv` promises (`-p`, `--model haiku`,
`--setting-sources project`, a `--settings` JSON naming `<cwd>/mem` or `<cwd>/mem-lines`,
`--strict-mcp-config`, `--tools Read`, `--max-budget-usd`,
`--output-format stream-json`, `--verbose`, and never
`--no-session-persistence`), exiting 3 on any mismatch -- a broken argv then
makes both runs exit non-zero and every probe VOID. It also reads the
fixture's own `.claude/settings.json` and refuses (exit 3) unless
`InstructionsLoaded` and `PreToolUse "*"` both register a real hook command,
then INVOKES those two registered commands directly -- piping
`InstructionsLoaded`/`PreToolUse` payloads to `hooks/log_loaded.py` and
`hooks/contain.py` rather than writing `loaded.jsonl` itself, so the
fixture's real hook scripts are what get exercised.

Hermetic throughout: every subprocess env is built from scratch (never
`dict(os.environ)`) -- `PATH`, `HOME`, `USERPROFILE`, `TMPDIR`,
`CLAUDE_TOOLKIT_CONFIG`, and all four `HARNESS_CHECK_*` seams point at a
per-test temp directory. `TMPDIR` is what lets the default mode's hardcoded
`tempfile.gettempdir()` be redirected without the executable needing a test
seam of its own -- `tempfile.gettempdir()` reads `TMPDIR` from the process
environment it is handed, and a subprocess env built from scratch never
carries the host's real one.

`PATH` holds the stub directory, then a per-test directory with a `python3`
symlink to `sys.executable`: the hook's shebang is `#!/usr/bin/env python3`,
`env` resolves that through the child's own `PATH`, and the interpreter
running the tests is not necessarily named `python3`.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, TOOL_DIR)
import harness_lib as lib  # noqa: E402

EXECUTABLE = os.path.join(TOOL_DIR, "claude-harness-check")


# ------------------------------------------------------------------- the stub --

STUB_SOURCE = """#!/usr/bin/env python3
# Stub `claude` for harness-check's runner tests -- see test_runner.py's
# module docstring. Emulates just enough of `claude -p --output-format
# stream-json` to drive evaluate(); never a real Claude Code invocation.
#
# Two things this stub enforces rather than ignores (Code Review Fix 2):
# it validates its own argv against the exact flags `_probe_argv` promises
# -- exiting 3 on any mismatch, so a broken argv makes every probe VOID --
# and it reads the fixture's own `.claude/settings.json`, refusing (exit 3)
# unless both real hook scripts are registered as the plan describes, then
# actually INVOKES them: InstructionsLoaded/PreToolUse payloads are piped to
# `hooks/log_loaded.py` and `hooks/contain.py` rather than this stub writing
# loaded.jsonl itself, so the fixture's real hooks are what get exercised.
import json
import os
import re
import subprocess
import sys


def project_slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(path))


def fail(message):
    sys.stderr.write("stub claude: " + message + "\\n")
    sys.exit(3)


marker = os.environ.get("HARNESS_CHECK_STUB_MARKER")
if marker:
    with open(marker, "a") as handle:
        handle.write("invoked\\n")

argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    sys.stdout.write("9.9.9 (Claude Code)\\n")
    sys.exit(0)

# ---- enforce the probe argv -----------------------------------------------

if "--no-session-persistence" in argv:
    fail("--no-session-persistence must never be passed -- both runs need a transcript")
if "-p" not in argv:
    fail("missing -p")


def value_after(flag):
    if flag not in argv:
        return None
    idx = argv.index(flag)
    return argv[idx + 1] if idx + 1 < len(argv) else None


if value_after("--model") != "haiku":
    fail("missing or wrong --model (want haiku)")
if value_after("--setting-sources") != "project":
    fail("missing or wrong --setting-sources (want project)")
if "--strict-mcp-config" not in argv:
    fail("missing --strict-mcp-config")
if value_after("--tools") != "Read":
    fail("missing or wrong --tools (want Read)")
if "--max-budget-usd" not in argv:
    fail("missing --max-budget-usd")
if value_after("--output-format") != "stream-json":
    fail("missing or wrong --output-format (want stream-json)")
if "--verbose" not in argv:
    fail("missing --verbose")

settings_raw = value_after("--settings")
if not settings_raw:
    fail("missing --settings")
try:
    settings_flag = json.loads(settings_raw)
except Exception:
    fail("--settings is not valid JSON")

cwd = os.getcwd()
mem_dir_name = None
for candidate in ("mem", "mem-lines"):
    if settings_flag.get("autoMemoryDirectory") == os.path.join(cwd, candidate):
        mem_dir_name = candidate
if mem_dir_name is None:
    fail("--settings autoMemoryDirectory does not name <cwd>/mem or <cwd>/mem-lines")
is_r3 = mem_dir_name == "mem-lines"

# ---- read the fixture's own settings.json, and use its real hooks --------


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def find_command(hooks_doc, event, matcher=None):
    for group in (hooks_doc or {}).get(event) or []:
        if matcher is not None and group.get("matcher") != matcher:
            continue
        for entry in group.get("hooks") or []:
            if entry.get("type") == "command" and entry.get("command"):
                return entry["command"]
    return None


fixture_settings = read_json(os.path.join(cwd, ".claude", "settings.json"))
hooks_doc = (fixture_settings or {}).get("hooks")
instr_command = find_command(hooks_doc, "InstructionsLoaded")
contain_command = find_command(hooks_doc, "PreToolUse", matcher="*")

if not instr_command or "hooks/log_loaded.py" not in instr_command:
    fail("fixture .claude/settings.json: no InstructionsLoaded -> hooks/log_loaded.py")
if not contain_command or "hooks/contain.py" not in contain_command:
    fail("fixture .claude/settings.json: no PreToolUse '*' -> hooks/contain.py")


def invoke_hook(command, payload):
    # A real subprocess running the fixture's real hook script -- shell=True
    # because the registered command is the shell-quoted string
    # `_settings_json` in harness_lib.py builds ("<python>" "<script>").
    try:
        subprocess.run(command, shell=True, input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass


mode = os.environ.get("STUB_MODE", "healthy")
disabled = bool(os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"))
is_r2 = disabled


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return None


claude_md = read(os.path.join(cwd, "CLAUDE.md")) or ""
memory_md = read(os.path.join(cwd, mem_dir_name, "MEMORY.md")) or ""
topic_md = read(os.path.join(cwd, "mem", "topics", "t.md")) or ""

# The real harness cut: 200 lines, THEN 25,000 characters.
cut_by_lines = "\\n".join(memory_md.split("\\n")[:200])
cut_memory = cut_by_lines[:25000]
if mode == "uncut-lines" and is_r3:
    cut_memory = memory_md[:25000]

model = ("claude-opus-4-1-99999" if mode == "opus"
        else "claude-haiku-4-5-20251001")
session_id = "stub-{0}-{1}".format(mode, os.getpid())

# The real binary writes under $CLAUDE_CONFIG_DIR/projects when that is set,
# else ~/.claude/projects; HARNESS_CHECK_PROJECTS_ROOT is the test seam.
projects_root = (os.environ.get("HARNESS_CHECK_PROJECTS_ROOT")
                 or os.path.join(os.environ.get("CLAUDE_CONFIG_DIR")
                                 or os.path.expanduser("~/.claude"), "projects"))
slug = project_slug(cwd)

# Emulate the per-session scratchpad directory a real child `claude -p`
# creates on 2.1.280 (see harness_lib._cleanup_scratchpad) -- unless asked
# to report a FOREIGN one instead, to prove the cleanup guard leaves that
# alone. `session_id` is overridden in that branch, before `transcript_path`
# below is built from it, so the reported scratchpad's own session
# component and the transcript's session_id always agree.
stub_tmp = os.environ.get("HARNESS_CHECK_STUB_TMP")
foreign_scratch = os.environ.get("HARNESS_CHECK_STUB_FOREIGN_SCRATCH")
scratchpad_path = None
if mode == "foreign-scratch" and foreign_scratch:
    scratchpad_path = foreign_scratch
    session_id = os.path.basename(os.path.dirname(foreign_scratch))
elif stub_tmp:
    scratchpad_dir = os.path.join(stub_tmp, slug, session_id, "scratchpad")
    os.makedirs(scratchpad_dir, exist_ok=True)
    scratchpad_path = scratchpad_dir

pdir = os.path.join(projects_root, slug)
os.makedirs(pdir, exist_ok=True)
transcript_path = os.path.join(pdir, session_id + ".jsonl")

instr_files = [{"path": os.path.join(cwd, "CLAUDE.md"), "type": "Project",
               "content": claude_md}]
if not disabled:
    instr_files.append({"path": os.path.join(cwd, mem_dir_name, "MEMORY.md"),
                        "type": "AutoMem", "content": cut_memory})

init_record = {"type": "system", "subtype": "init", "session_id": session_id,
              "model": model, "claude_code_version": "9.9.9"}
if scratchpad_path:
    init_record["scratchpad_path"] = scratchpad_path

transcript_lines = [
    init_record,
    {"type": "attachment",
     "attachment": {"type": "instructions", "files": instr_files}},
]
if mode == "leak-topic":
    transcript_lines.append({"type": "user", "text": topic_md})

with open(transcript_path, "w", encoding="utf-8") as handle:
    for line in transcript_lines:
        handle.write(json.dumps(line) + "\\n")

# The fixture's real hooks, exercised through their own scripts rather than
# this stub writing loaded.jsonl directly.
invoke_hook(instr_command, {
    "file_path": os.path.join(cwd, ".claude", "rules", "always.md"),
    "load_reason": "session_start"})
if mode != "scoped-r2-only" or is_r2:
    invoke_hook(instr_command, {
        "file_path": os.path.join(cwd, ".claude", "rules", "scoped.md"),
        "load_reason": "path_glob_match"})
invoke_hook(contain_command, {
    "tool_name": "Read",
    "tool_input": {"file_path": os.path.join(cwd, "src", "x.py")}})

sys.stdout.write(json.dumps(transcript_lines[0]) + "\\n")
sys.stdout.write(json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Read",
                             "input": {"file_path": os.path.join(cwd, "src", "x.py")}}]},
}) + "\\n")
sys.stdout.write(json.dumps({"type": "result", "subtype": "success"}) + "\\n")
sys.stdout.flush()

sys.exit(1 if mode == "exit1" or (mode == "r3-exit1" and is_r3) else 0)
"""


def _write_stub(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(STUB_SOURCE)
    os.chmod(path, 0o755)


# --------------------------------------------------------------------- fixture --

class RunnerCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-check-runner-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.python_dir = os.path.join(self.tmp, "pybin")
        os.makedirs(self.python_dir)
        os.symlink(sys.executable, os.path.join(self.python_dir, "python3"))

        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, exist_ok=True)
        self.tmp_parent = os.path.join(self.tmp, "tmp-parent")
        os.makedirs(self.tmp_parent, exist_ok=True)
        self.projects_root = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects_root, exist_ok=True)
        self.results_path = os.path.join(self.tmp, "results.json")
        self.user_settings_path = os.path.join(self.tmp, "user-settings.json")
        self.marker_path = os.path.join(self.tmp, "stub-invoked.log")
        self.stub_tmp = os.path.join(self.tmp, "stub-tmp")
        os.makedirs(self.stub_tmp, exist_ok=True)

        self.stub_dir = os.path.join(self.tmp, "bin")
        os.makedirs(self.stub_dir, exist_ok=True)
        self.stub_path = os.path.join(self.stub_dir, "claude")
        _write_stub(self.stub_path)

    # --- environment/subprocess helpers ---------------------------------

    def env(self, stub_mode=None, extra=None, with_stub_on_path=True, drop=()):
        """Built from scratch -- never `dict(os.environ)`. `CLAUDE_CODE_EXECPATH`
        is never included, so binary resolution always falls through to
        whatever `claude` this PATH resolves (the stub, or nothing). `drop`
        removes named seams, so a test can exercise the production default.
        """
        path = os.pathsep.join(
            ([self.stub_dir] if with_stub_on_path else []) + [self.python_dir])
        base = {
            "PATH": path,
            "HOME": self.home,
            "USERPROFILE": self.home,
            "TMPDIR": self.tmp_parent,
            "CLAUDE_TOOLKIT_CONFIG": "/nonexistent-config.json",
            "HARNESS_CHECK_STATE_PATH": self.results_path,
            "HARNESS_CHECK_PROJECTS_ROOT": self.projects_root,
            "HARNESS_CHECK_USER_SETTINGS": self.user_settings_path,
            "HARNESS_CHECK_STUB_MARKER": self.marker_path,
            "HARNESS_CHECK_STUB_TMP": self.stub_tmp,
        }
        if stub_mode is not None:
            base["STUB_MODE"] = stub_mode
        if extra:
            base.update(extra)
        for key in drop:
            base.pop(key, None)
        return base

    def run_executable(self, args=None, **env_kwargs):
        return subprocess.run(
            [EXECUTABLE] + (args or []), capture_output=True, text=True,
            env=self.env(**env_kwargs), timeout=60)

    def read_results(self):
        with open(self.results_path) as handle:
            return json.load(handle)

    def probe_lines(self, stdout, probe_id=None):
        lines = [l for l in stdout.splitlines() if l.startswith("H")]
        if probe_id:
            lines = [l for l in lines if l.split()[0] == probe_id]
        return lines


# ============================================================== acceptance 1 --

class TestHealthy(RunnerCase):

    def test_healthy_without_a_session_id_is_partial_and_cleans_up(self):
        done = self.run_executable(stub_mode="healthy")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        lines = self.probe_lines(done.stdout)
        self.assertEqual(len(lines), 6)
        for line in lines:
            expected = "PARTIAL" if line.split()[0] == "H5" else "PASS"
            self.assertEqual(line.split()[1], expected, line)
        self.assertEqual(self.probe_lines(done.stdout, "H6")[0].split()[1], "PASS")
        self.assertIn("version 9.9.9: partial", done.stdout)
        self.assertIn("user-level memory resolution NOT verified", done.stdout)
        with open(self.marker_path) as handle:
            self.assertEqual(len(handle.read().splitlines()), 3)

        record = self.read_results()["versions"]["9.9.9"]
        self.assertEqual(record["result"], "partial")
        self.assertEqual(record["unverified"], ["H5"])
        self.assertEqual(record["rev"], 3)
        self.assertEqual(record["failures"], [])

        # Both the fixture's own temp dir and its projects directory are
        # gone -- acceptance 1's cleanup half.
        self.assertEqual(os.listdir(self.tmp_parent), [])
        self.assertEqual(os.listdir(self.projects_root), [])


# ============================================================== acceptance 2 --

class TestLeakTopic(RunnerCase):

    def test_leak_topic_fails_h2_and_overwrites_an_existing_pass(self):
        seeded = lib.record_result({}, "9.9.9", "pass", [], "2020-01-01T00:00:00Z")
        lib.save_json(self.results_path, seeded)

        done = self.run_executable(stub_mode="leak-topic")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

        h2_lines = self.probe_lines(done.stdout, "H2")
        self.assertEqual(len(h2_lines), 1)
        self.assertEqual(h2_lines[0].split()[1], "FAIL")

        state = self.read_results()
        self.assertEqual(state["versions"]["9.9.9"]["result"], "fail")


# ============================================================== acceptance 3 --

class TestOpus(RunnerCase):

    def test_opus_every_probe_void_state_unchanged(self):
        done = self.run_executable(stub_mode="opus")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)

        lines = self.probe_lines(done.stdout)
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertEqual(line.split()[1], "VOID", line)

        self.assertFalse(os.path.exists(self.results_path))


# ============================================================== acceptance 4 --

class TestExit1(RunnerCase):

    def test_exit1_exits_two(self):
        done = self.run_executable(stub_mode="exit1")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)


class TestR3Modes(RunnerCase):

    def test_uncut_lines_fails_h6_only(self):
        done = self.run_executable(stub_mode="uncut-lines")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for line in self.probe_lines(done.stdout):
            expected = {"H5": "PARTIAL", "H6": "FAIL"}.get(line.split()[0], "PASS")
            self.assertEqual(line.split()[1], expected, line)
        record = self.read_results()["versions"]["9.9.9"]
        self.assertEqual(record["result"], "fail")
        self.assertEqual(record["failures"], ["H6"])

    def test_r3_exit1_voids_h6_and_leaves_results_byte_identical(self):
        seeded = lib.record_result({}, "9.9.9", "pass", [], "2020-01-01T00:00:00Z")
        lib.save_json(self.results_path, seeded)
        with open(self.results_path, "rb") as handle:
            before = handle.read()

        done = self.run_executable(stub_mode="r3-exit1")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        h6 = self.probe_lines(done.stdout, "H6")
        self.assertEqual(len(h6), 1)
        self.assertEqual(h6[0].split()[1], "VOID")
        self.assertIn("R3 unusable", h6[0])
        with open(self.results_path, "rb") as handle:
            self.assertEqual(handle.read(), before)


# ============================================================= acceptance 4b --

class TestScopedR2Only(RunnerCase):

    def test_scoped_r2_only_fails_h3_because_logs_are_not_pooled(self):
        done = self.run_executable(stub_mode="scoped-r2-only")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

        h3_lines = self.probe_lines(done.stdout, "H3")
        self.assertEqual(len(h3_lines), 1)
        self.assertEqual(h3_lines[0].split()[1], "FAIL")


# ============================================================== acceptance 5 --

class TestPreexistingProjectsDirInProcess(RunnerCase):

    def test_preexisting_projects_dir_voids_everything_stub_never_ran(self):
        fixed_parent = os.path.join(self.tmp, "fixed-fixture-parent")
        os.makedirs(fixed_parent, exist_ok=True)
        fixed_root = tempfile.mkdtemp(prefix="harness-check-", dir=fixed_parent)
        real_root = os.path.realpath(fixed_root)

        original_mkdtemp = lib.tempfile.mkdtemp
        lib.tempfile.mkdtemp = lambda *a, **k: fixed_root

        old_root_env = os.environ.get("HARNESS_CHECK_PROJECTS_ROOT")
        os.environ["HARNESS_CHECK_PROJECTS_ROOT"] = self.projects_root
        try:
            pdir = lib.projects_dir_for(real_root)
            os.makedirs(pdir, exist_ok=True)

            fx, r1, r2, calling, r3 = lib.run_probes(
                self.stub_path, fixed_parent, None, self.env())
            version, results = lib.evaluate(fx, r1, r2, calling, r3)
            status, failures, exit_code = lib.aggregate(version, results)

            self.assertEqual(exit_code, 2)
            for r in results:
                self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertFalse(os.path.exists(self.marker_path),
                             "the stub wrote its marker -- it must never have run")
            self.assertTrue(os.path.exists(pdir),
                            "a directory this run did not own must survive")
        finally:
            lib.tempfile.mkdtemp = original_mkdtemp
            if old_root_env is None:
                os.environ.pop("HARNESS_CHECK_PROJECTS_ROOT", None)
            else:
                os.environ["HARNESS_CHECK_PROJECTS_ROOT"] = old_root_env


# ============================================================== acceptance 6 --

class TestStatus(RunnerCase):

    def test_status_after_healthy_prints_version_and_partial(self):
        done = self.run_executable(stub_mode="healthy")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        status = self.run_executable(args=["--status"])
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("9.9.9", status.stdout)
        self.assertIn("partial", status.stdout)
        self.assertIn("unverified=H5", status.stdout)

    def test_status_prints_unverified_for_a_seeded_partial_record(self):
        seeded = lib.record_result({}, "9.9.9", "partial", [],
                                   "2020-01-01T00:00:00Z", unverified=["H5"])
        lib.save_json(self.results_path, seeded)
        status = self.run_executable(args=["--status"])
        line = [l for l in status.stdout.splitlines() if l.startswith("9.9.9")][0]
        self.assertIn("partial", line)
        self.assertIn("unverified=H5", line)

    def _status_line(self, record):
        lib.save_json(self.results_path, {"versions": {"9.9.9": record}})
        status = self.run_executable(args=["--status"])
        return [l for l in status.stdout.splitlines() if l.startswith("9.9.9")][0]

    def test_status_marks_a_rev_2_record_as_legacy(self):
        line = self._status_line({"result": "pass", "checked_at": "2020-01-01T00:00:00Z",
                                  "failures": [], "rev": 2})
        self.assertIn("legacy rev 2 \u2014 not verified by the current check", line)
        self.assertTrue(line.endswith(")"), line)

    def test_status_marks_a_record_with_no_rev_as_legacy_none(self):
        line = self._status_line({"result": "pass", "checked_at": "2020-01-01T00:00:00Z",
                                  "failures": []})
        self.assertIn("legacy rev none", line)

    def test_status_prints_no_legacy_text_at_the_current_rev(self):
        line = self._status_line({"result": "pass", "checked_at": "2020-01-01T00:00:00Z",
                                  "failures": [], "rev": lib.RECORD_REV})
        self.assertNotIn("legacy", line)

    def test_status_prints_no_unverified_for_a_pass_record(self):
        seeded = lib.record_result({}, "9.9.9", "pass", [], "2020-01-01T00:00:00Z")
        lib.save_json(self.results_path, seeded)
        status = self.run_executable(args=["--status"])
        self.assertNotIn("unverified", status.stdout)


# ============================================================= acceptance 6b/6c --

class TestCallingSession(RunnerCase):

    def _write_calling_transcript(self, automem_dir, projects_root=None,
                                  with_memory=True):
        """The calling session's transcript. `with_memory=False` is what a
        healthy harness records when there was nothing to load: no
        MEMORY.md on disk and no `AutoMem` entry.
        """
        session_dir = os.path.join(projects_root or self.projects_root, "-x")
        os.makedirs(session_dir, exist_ok=True)
        transcript_path = os.path.join(session_dir, "abc.jsonl")
        automem_path = os.path.join(automem_dir, "MEMORY.md")
        content = "calling session memory content\n"
        files = []
        if with_memory:
            with open(automem_path, "w", encoding="utf-8") as handle:
                handle.write(content)
            files.append({"path": automem_path, "type": "AutoMem", "content": content})
        lines = [
            {"type": "user", "version": "9.9.9"},
            {"type": "attachment", "attachment": {"type": "instructions", "files": files}},
        ]
        with open(transcript_path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")

    def _write_user_settings(self, automem_dir, path=None, **more):
        with open(path or self.user_settings_path, "w", encoding="utf-8") as handle:
            json.dump(dict({"autoMemoryDirectory": automem_dir}, **more), handle)

    def _h5_line(self, done):
        h5_lines = self.probe_lines(done.stdout, "H5")
        self.assertEqual(len(h5_lines), 1, done.stdout + done.stderr)
        return h5_lines[0]

    # --- Merge Review MR1: nothing to load is a SKIP, never a FAIL ---------
    # Each case writes the transcript a healthy harness produces when there
    # is nothing to load (no AutoMem entry). Before the fix every one of these
    # exited 1 with H5 FAIL; `test_mismatched_directory_fails_h5` is the
    # control that (c) still fails when there IS something to load.

    def test_empty_memory_directory_skips_h5c(self):
        automem_dir = os.path.join(self.tmp, "user-mem")
        os.makedirs(automem_dir, exist_ok=True)
        self._write_calling_transcript(automem_dir, with_memory=False)
        self._write_user_settings(automem_dir)

        done = self.run_executable(
            stub_mode="healthy", extra={"CLAUDE_CODE_SESSION_ID": "abc"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("user-level: SKIP (no MEMORY.md to load", self._h5_line(done))

    def test_auto_memory_disabled_by_env_skips_h5c(self):
        automem_dir = os.path.join(self.tmp, "user-mem")
        os.makedirs(automem_dir, exist_ok=True)
        with open(os.path.join(automem_dir, "MEMORY.md"), "w") as handle:
            handle.write("present but not loaded\n")
        self._write_calling_transcript(automem_dir, with_memory=False)
        self._write_user_settings(automem_dir)

        done = self.run_executable(stub_mode="healthy", extra={
            "CLAUDE_CODE_SESSION_ID": "abc",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "TRUE"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("user-level: SKIP (auto-memory disabled in the calling session)",
                      self._h5_line(done))

    def test_auto_memory_enabled_false_skips_h5c(self):
        automem_dir = os.path.join(self.tmp, "user-mem")
        os.makedirs(automem_dir, exist_ok=True)
        with open(os.path.join(automem_dir, "MEMORY.md"), "w") as handle:
            handle.write("present but not loaded\n")
        self._write_calling_transcript(automem_dir, with_memory=False)
        self._write_user_settings(automem_dir, autoMemoryEnabled=False)

        done = self.run_executable(
            stub_mode="healthy", extra={"CLAUDE_CODE_SESSION_ID": "abc"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("user-level: SKIP (autoMemoryEnabled is false)", self._h5_line(done))

    def test_no_entry_despite_memory_to_load_still_fails_h5c(self):
        """The control for the three skips: enabled, MEMORY.md present, and
        the calling session loaded nothing -- a real harness failure.
        """
        automem_dir = os.path.join(self.tmp, "user-mem")
        os.makedirs(automem_dir, exist_ok=True)
        with open(os.path.join(automem_dir, "MEMORY.md"), "w") as handle:
            handle.write("should have been loaded\n")
        self._write_calling_transcript(automem_dir, with_memory=False)
        self._write_user_settings(automem_dir)

        done = self.run_executable(
            stub_mode="healthy", extra={"CLAUDE_CODE_SESSION_ID": "abc"})
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self._h5_line(done).split()[1], "FAIL")

    # --- Merge Review MR2: CLAUDE_CONFIG_DIR is where the child writes -----

    def test_claude_config_dir_is_read_for_projects_and_settings(self):
        """No HARNESS_CHECK_PROJECTS_ROOT / _USER_SETTINGS seam: the stub, like
        the real binary, writes under $CLAUDE_CONFIG_DIR/projects. The runner
        must read R1/R2 there (else every probe VOIDs, exit 2), read the
        calling session's settings and transcript there (else H5 SKIPs), and
        clean up there (else the projects dir leaks).
        """
        config_dir = os.path.join(self.tmp, "relocated-config")
        cfg_projects = os.path.join(config_dir, "projects")
        os.makedirs(cfg_projects, exist_ok=True)
        automem_dir = os.path.join(self.tmp, "user-mem")
        os.makedirs(automem_dir, exist_ok=True)
        self._write_calling_transcript(automem_dir, projects_root=cfg_projects)
        self._write_user_settings(
            automem_dir, path=os.path.join(config_dir, "settings.json"))

        done = self.run_executable(
            stub_mode="healthy",
            extra={"CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_CONFIG_DIR": config_dir},
            drop=("HARNESS_CHECK_PROJECTS_ROOT", "HARNESS_CHECK_USER_SETTINGS"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for line in self.probe_lines(done.stdout):
            self.assertEqual(line.split()[1], "PASS", line)
        self.assertNotIn("SKIP", self._h5_line(done))
        self.assertEqual(sorted(os.listdir(cfg_projects)), ["-x"],
                         "the run's own projects dir leaked under CLAUDE_CONFIG_DIR")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude")),
                         "something wrote under ~/.claude despite CLAUDE_CONFIG_DIR")

    def test_matching_directory_h5_has_no_skip_and_passes(self):
        automem_dir = os.path.join(self.tmp, "user-mem")
        os.makedirs(automem_dir, exist_ok=True)
        self._write_calling_transcript(automem_dir)
        self._write_user_settings(automem_dir)

        done = self.run_executable(
            stub_mode="healthy", extra={"CLAUDE_CODE_SESSION_ID": "abc"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        h5_lines = self.probe_lines(done.stdout, "H5")
        self.assertEqual(len(h5_lines), 1)
        self.assertNotIn("SKIP", h5_lines[0])
        # The verified-PASS control: (c) resolved, so the record is a full
        # pass, current rev, nothing unverified.
        self.assertEqual(h5_lines[0].split()[1], "PASS")
        record = self.read_results()["versions"]["9.9.9"]
        self.assertEqual(record["result"], "pass")
        self.assertEqual(record["rev"], 3)
        self.assertNotIn("unverified", record)

    def test_mismatched_directory_fails_h5(self):
        automem_dir = os.path.join(self.tmp, "user-mem")
        other_dir = os.path.join(self.tmp, "other-mem")
        os.makedirs(automem_dir, exist_ok=True)
        os.makedirs(other_dir, exist_ok=True)
        self._write_calling_transcript(automem_dir)
        self._write_user_settings(other_dir)

        done = self.run_executable(
            stub_mode="healthy", extra={"CLAUDE_CODE_SESSION_ID": "abc"})
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

        h5_lines = self.probe_lines(done.stdout, "H5")
        self.assertEqual(len(h5_lines), 1)
        self.assertEqual(h5_lines[0].split()[1], "FAIL")

    def test_no_session_id_makes_h5_partial_with_the_named_reason(self):
        done = self.run_executable(stub_mode="healthy")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        h5_lines = self.probe_lines(done.stdout, "H5")
        self.assertEqual(len(h5_lines), 1)
        self.assertEqual(h5_lines[0].split()[1], "PARTIAL")
        self.assertIn("user-level: NOT VERIFIED (not run from a Claude session)",
                      h5_lines[0])
        self.assertIn("user-level memory resolution NOT verified", done.stdout)
        record = self.read_results()["versions"]["9.9.9"]
        self.assertEqual(record["result"], "partial")
        self.assertEqual(record["unverified"], ["H5"])
        self.assertEqual(record["rev"], 3)
        self.assertEqual(record["failures"], [])


# ============================================================== acceptance 7 --

class TestNoBinary(RunnerCase):

    def test_no_binary_on_path_or_execpath_exits_two_with_the_message(self):
        done = self.run_executable(with_stub_on_path=False)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("cannot find a claude binary", done.stdout + done.stderr)


# ==================================================== unrecorded verdict is loud --
# Code Review Fix 1: `save_json`'s return value must not be ignored. When the
# results write fails, the verdict must never be silently unstored -- print
# the per-probe lines (already unconditional), then a loud stderr error and
# exit 2, regardless of what the (unstored) verdict was.

class TestUnrecordedVerdictIsLoud(RunnerCase):

    def setUp(self):
        super().setUp()
        # A directory the results file lives in, seeded with a `pass` for
        # 9.9.9, then made unwritable (0555: read+execute, no write) so
        # `save_json`'s own `tempfile.mkstemp(dir=directory, ...)` fails.
        self.readonly_dir = os.path.join(self.tmp, "readonly-results")
        os.makedirs(self.readonly_dir, exist_ok=True)
        self.readonly_results_path = os.path.join(self.readonly_dir, "results.json")
        seeded = lib.record_result({}, "9.9.9", "pass", [], "2020-01-01T00:00:00Z")
        self.assertTrue(lib.save_json(self.readonly_results_path, seeded))
        os.chmod(self.readonly_dir, 0o555)

    def tearDown(self):
        # Restore permissions before the temp-dir cleanup in the parent's
        # addCleanup runs, or shutil.rmtree cannot remove the directory.
        os.chmod(self.readonly_dir, 0o755)
        super().tearDown()

    def test_save_failure_is_loud_exits_2_and_leaves_the_stored_pass_intact(self):
        done = self.run_executable(
            stub_mode="leak-topic",
            extra={"HARNESS_CHECK_STATE_PATH": self.readonly_results_path})

        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)

        # The per-probe lines still print first, even though the verdict
        # they describe (a "fail") is never actually recorded.
        h2_lines = self.probe_lines(done.stdout, "H2")
        self.assertEqual(len(h2_lines), 1)
        self.assertEqual(h2_lines[0].split()[1], "FAIL")

        self.assertIn(
            "ERROR: could not record the result to {0}; the verdict above "
            "is NOT stored".format(self.readonly_results_path), done.stderr)

        with open(self.readonly_results_path) as handle:
            state = json.load(handle)
        self.assertEqual(state["versions"]["9.9.9"]["result"], "pass")


# ============================================== scratchpad leak, its cleanup --
# Defect found live: 2.1.280 has every child `claude -p` create its own
# per-session scratchpad directory (`init.scratchpad_path`), and `run_probes`
# never removed it -- one leaked `<slug>` directory per check. These two
# classes cover the fix from both directions: a healthy run's own scratchpad
# is gone and an unrelated sibling directory under the same tmp root is left
# alone, and a scratchpad reporting a FOREIGN slug is never touched at all.

class TestScratchpadCleanup(RunnerCase):

    def test_healthy_run_removes_its_scratchpad_and_leaves_a_sibling(self):
        sibling = os.path.join(self.stub_tmp, "not-ours-slug")
        os.makedirs(sibling, exist_ok=True)
        with open(os.path.join(sibling, "keep.txt"), "w") as handle:
            handle.write("do not touch\n")

        done = self.run_executable(stub_mode="healthy")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        with open(self.marker_path) as handle:
            self.assertEqual(len(handle.read().splitlines()), 3,
                             "three runs, so three scratchpads were created")
        remaining = sorted(os.listdir(self.stub_tmp))
        self.assertEqual(remaining, ["not-ours-slug"],
                         "the fixture's own scratchpad slug directory must "
                         "be gone, and only the sibling survives")
        self.assertTrue(os.path.exists(os.path.join(sibling, "keep.txt")),
                        "a directory the test created must never be touched")


class TestForeignScratchpadGuard(RunnerCase):

    def test_foreign_slug_scratchpad_is_never_removed(self):
        foreign_root = os.path.join(self.tmp, "foreign-root")
        foreign_sess_dir = os.path.join(
            foreign_root, "not-the-fixture-slug", "foreign-session")
        foreign_scratchpad = os.path.join(foreign_sess_dir, "scratchpad")
        os.makedirs(foreign_scratchpad, exist_ok=True)
        with open(os.path.join(foreign_scratchpad, "keep.txt"), "w") as handle:
            handle.write("do not touch\n")

        done = self.run_executable(
            stub_mode="foreign-scratch",
            extra={"HARNESS_CHECK_STUB_FOREIGN_SCRATCH": foreign_scratchpad})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        self.assertTrue(
            os.path.exists(foreign_scratchpad),
            "a scratchpad whose slug does not match the fixture's own must "
            "never be removed")
        self.assertTrue(os.path.exists(os.path.join(foreign_scratchpad, "keep.txt")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
