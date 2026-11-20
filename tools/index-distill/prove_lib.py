"""claude-index-distill prove: the proof fixture and its structural check.

`build_fixture` copies live memory and the workspace's rule pages into a
throwaway root, applies the manifest there, and fences the root with a
containment hook. Nothing here touches live memory or the live workspace.
"""

import contextlib
import datetime
import hashlib
import json
import math
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile

# realpath, not abspath: this is reached through a ~/.local/bin symlink.
_HERE = os.path.dirname(os.path.realpath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_ROOT, "lib"),
           os.path.join(_ROOT, "tools", "tool-rules"),
           os.path.join(_ROOT, "tools", "memory-lint"),
           os.path.join(_ROOT, "tools", "harness-check"),
           _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import distill_claim  # noqa: E402
import distill_lib  # noqa: E402
import harness_lib  # noqa: E402
import lint_lib  # noqa: E402
import portable  # noqa: E402
import tool_rules_lib  # noqa: E402
import toolkit_config  # noqa: E402

TOKEN_PREFIX = "IDXCHECK-"
_TOOL_RULES_CMD = "tools/tool-rules/claude-tool-rules"

# Any of these selects API billing or another provider in place of the
# subscription; no child or grader ever inherits one.
PAID_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                  "CLAUDE_CODE_USE_FOUNDRY")
HAIKU_TIMEOUT = 300
OPUS_TIMEOUT = 900
STOP_AT = 0.85


class FixtureError(Exception):
    pass


class Terminated(BaseException):
    """Raised by the CLI's SIGTERM/SIGHUP handler so `finally` blocks run."""


class Fixture(object):
    def __init__(self, root):
        self.root = root
        self.mem = os.path.join(root, "mem")
        self.proof = os.path.join(root, "_proof")
        self.tokens = {}
        self.pages = []
        self.surface = {}


def fixture_base(config=None):
    """Where fixtures are made: index_distill.fixture_root, else <tmp>/claude-index-distill."""
    configured = (config or toolkit_config).get("index_distill", "fixture_root")
    if isinstance(configured, str) and configured:
        base = toolkit_config.expand_path(configured) or configured
    else:
        base = os.path.join(tempfile.gettempdir(), "claude-index-distill")
    os.makedirs(base, exist_ok=True)
    return base


# Exactly what mkdtemp names a fixture or grader dir: its prefix plus 8 random chars.
# Used with fullmatch: `$` would also accept a trailing newline.
_ORPHAN_NAME = re.compile(r"index-distill-(grader-)?[a-z0-9_]{8}")
PROVE_LOCK = ".prove.lock"


def sweep_orphans(base_dir):
    """Remove fixture and grader dirs an earlier proof left under `base_dir`.

    dispose() cannot run after SIGKILL, a second SIGTERM mid-cleanup, or a signal
    between build_fixture returning and prove recording the root; each leaves a
    copy of real memory behind. prove calls it only while holding PROVE_LOCK and
    before making its own dirs, so no running proof's dir can match. Real
    directories only, never a symlink. Returns (removed, failed) path lists."""
    removed, failed = [], []
    try:
        with os.scandir(base_dir) as entries:
            found = sorted(e.path for e in entries
                           if _ORPHAN_NAME.fullmatch(e.name) and e.is_dir(follow_symlinks=False))
    except OSError:
        return removed, [base_dir]
    for path in found:
        try:
            shutil.rmtree(path)
            removed.append(path)
        except OSError:
            failed.append(path)
    return removed, failed


def production_posttooluse(plugin_root=None):
    """The shipped tool-rules PostToolUse entry, ${CLAUDE_PLUGIN_ROOT} resolved."""
    if plugin_root is None:
        plugin_root = os.path.realpath(os.path.join(_ROOT, "plugin-continuity"))
    path = os.path.join(plugin_root, "hooks", "hooks.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data["hooks"]["PostToolUse"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FixtureError("cannot read PostToolUse entries from %s: %s" % (path, exc))
    found = []
    for entry in entries:
        for hook in entry.get("hooks", []):
            if _TOOL_RULES_CMD in str(hook.get("command", "")):
                found.append((entry, hook))
    if len(found) != 1:
        raise FixtureError("expected exactly one tool-rules PostToolUse entry in %s, "
                           "found %d" % (path, len(found)))
    entry, hook = found[0]
    return {"matcher": entry.get("matcher"),
            "hooks": [{"type": "command",
                       "command": hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", plugin_root),
                       "timeout": hook.get("timeout")}]}


_CONTAIN_TEMPLATE = '''#!/usr/bin/env python3
# Fixture hook (PreToolUse): allows only the fixture's own reads, non-reserved
# writes and the exact probe commands; everything else exits 2. Stdlib only.
import json
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
    except (AttributeError, ValueError):
        pass

ROOT = __ROOT__
PROBES = __PROBES__


def inside(path):
    real = os.path.realpath(path if os.path.isabs(path) else os.path.join(ROOT, path))
    return real == ROOT or real.startswith(ROOT + os.sep), real


def reserved(real):
    parts = [p.casefold() for p in os.path.relpath(real, ROOT).replace("\\\\", "/").split("/")
             if p not in ("", ".")]
    if any(p in ("claude.md", "claude.local.md") for p in parts):
        return True
    if ".claude" in parts or (parts and parts[0] == "_proof"):
        return True
    norm = "/".join(parts)
    return norm == "mem" or norm.startswith("mem/")


def block(payload, name):
    try:
        with open(os.path.join(ROOT, "_proof", "blocked.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"tool_name": name,
                                 "tool_input": (payload or {}).get("tool_input")}) + "\\n")
    except Exception:
        pass
    sys.stderr.write("index-distill fixture: blocked %s\\n" % name)
    sys.exit(2)


try:
    payload = json.loads(sys.stdin.buffer.read())
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
except Exception:
    block(None, None)

name = payload.get("tool_name")
tool_input = payload.get("tool_input")
if not isinstance(tool_input, dict):
    tool_input = {}
allowed = False
if name == "Read":
    target = tool_input.get("file_path")
    allowed = isinstance(target, str) and bool(target) and inside(target)[0]
elif name in ("Glob", "Grep"):
    target = tool_input.get("path")
    allowed = target in (None, "") or (isinstance(target, str) and inside(target)[0])
elif name in ("Edit", "Write"):
    target = tool_input.get("file_path")
    if isinstance(target, str) and target:
        ok, real = inside(target)
        allowed = ok and not reserved(real)
elif name == "Bash":
    allowed = tool_input.get("command") in PROBES
if not allowed:
    block(payload, name)
sys.exit(0)
'''


def contain_script(root, probe_commands):
    """Text of `_proof/hooks/contain.py`, ROOT and PROBES baked in via repr."""
    return (_CONTAIN_TEMPLATE
            .replace("__ROOT__", repr(os.path.realpath(root)))
            .replace("__PROBES__", repr(list(probe_commands))))


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _tool_command(page):
    return page["probe"]["tool_input"]["command"]


def _walk(root):
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            yield rel, path


def _copy_tree(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=False)


def _build(fx, manifest, memory_dir, workspace, config, config_doc, today, rng):
    root = fx.root
    claude_md = os.path.join(workspace, "CLAUDE.md")
    if os.path.isfile(claude_md):
        shutil.copyfile(claude_md, os.path.join(root, "CLAUDE.md"))
    _copy_tree(os.path.join(workspace, ".claude", "rules"),
               os.path.join(root, ".claude", "rules"))
    _copy_tree(os.path.join(workspace, ".claude", "tool-rules"),
               os.path.join(root, ".claude", "tool-rules"))
    _copy_tree(memory_dir, fx.mem)
    distill_lib.apply_manifest(manifest, fx.mem, root, today)

    tool_pages = [p for p in manifest["pages"] if p["bucket"] == "tool"]
    probes = [_tool_command(p) for p in tool_pages]
    hooks = os.path.join(fx.proof, "hooks")
    _write(os.path.join(hooks, "log_loaded.py"),
           harness_lib._log_loaded_script(fx.proof), 0o755)
    _write(os.path.join(hooks, "contain.py"), contain_script(root, probes), 0o755)
    doc = dict(config_doc or {})
    doc["workspace_root"] = root
    _write(os.path.join(fx.proof, "config.json"), json.dumps(doc, indent=2) + "\n")
    os.makedirs(os.path.join(fx.proof, "state"), exist_ok=True)

    python_exe = sys.executable or "python3"

    def command(name):
        return "\"%s\" \"%s\"" % (python_exe, os.path.join(hooks, name))

    settings = {"hooks": {
        "InstructionsLoaded": [{"hooks": [
            {"type": "command", "command": command("log_loaded.py"), "timeout": 5}]}],
        "PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": command("contain.py"), "timeout": 5}]}],
        "PostToolUse": [production_posttooluse()],
    }}
    _write(os.path.join(root, ".claude", "settings.json"),
           json.dumps(settings, indent=2) + "\n")

    for page in manifest["pages"]:
        if page["bucket"] == "path":
            _write(str(distill_lib.resolve_inside(root, page["probe"]["path"])),
                   "probe\n", 0o755)
        else:
            script = distill_lib.probe_script(_tool_command(page))
            if script is None:
                raise FixtureError("%s: probe command has no stub-able script" % page["file"])
            first = _tool_command(page).split(" ")[0]
            text = 'print("ok")\n' if first == "python3" else "#!/bin/sh\necho ok\n"
            _write(str(distill_lib.resolve_inside(root, script)), text, 0o755)

    limit = tool_rules_lib.body_limit(tool_rules_lib.max_bytes(config))
    for page in manifest["pages"]:
        abs_path = str(distill_lib._page_path(page, root))
        fx.pages.append((page, abs_path))
        if page["bucket"] != "tool":
            continue
        token = None
        while token is None or token in fx.tokens.values():
            token = "%s%06X" % (TOKEN_PREFIX, rng.getrandbits(24))
        fx.tokens[page["file"]] = token
        with open(abs_path, "a", encoding="utf-8") as fh:
            fh.write("\nFixture check token: %s\n" % token)
        with open(abs_path, encoding="utf-8") as fh:
            parsed = tool_rules_lib.parse_page(fh.read())
        if parsed is not None and len(parsed[1].encode("utf-8")) > limit:
            raise FixtureError("%s is too close to the size cap for the proof "
                               "canary; trim it" % page["file"])
    fx.surface = surface_hashes(root)


def build_fixture(manifest, memory_dir, workspace, config, config_doc, today,
                  base_dir, rng):
    """A Fixture under `base_dir`. `config` is the config interface, `config_doc` the dict (G10).

    Never cleans up: on failure the exception carries `.root` for the caller.
    """
    root = os.path.realpath(tempfile.mkdtemp(prefix="index-distill-", dir=base_dir))
    fx = Fixture(root)
    try:
        _build(fx, manifest, memory_dir, workspace, config, config_doc, today, rng)
    except BaseException as exc:
        exc.root = root
        raise
    return fx


def surface_hashes(root):
    """{rel: sha256} of every reserved file outside `_proof/`: the instruction surface."""
    out = {}
    for rel, path in _walk(root):
        if rel.split("/")[0] == "_proof" or not distill_lib.is_reserved(rel, distill_lib.MEMDIR_REL):
            continue
        out[rel] = distill_lib.sha256_file(path)
    return out


def snapshot(root):
    """{rel: bytes} of every non-reserved file."""
    out = {}
    for rel, path in _walk(root):
        if distill_lib.is_reserved(rel, distill_lib.MEMDIR_REL):
            continue
        with open(path, "rb") as fh:
            out[rel] = fh.read()
    return out


def restore(root, snap):
    """Bring the non-reserved files back to `snap`; directories stay."""
    for rel, path in list(_walk(root)):
        if distill_lib.is_reserved(rel, distill_lib.MEMDIR_REL):
            continue
        if rel not in snap:
            os.remove(path)
    for rel, data in snap.items():
        path = os.path.join(root, *rel.split("/"))
        current = None
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                current = fh.read()
        if current != data:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)


def changed_files(root, snap):
    """{rel: text} of non-reserved files created or changed versus `snap`."""
    out = {}
    for rel, data in snapshot(root).items():
        if snap.get(rel) != data:
            out[rel] = data.decode("utf-8", errors="replace")
    return out


def structural(memory_dir, config):
    """(ok, detail): lint-clean and under the distill threshold."""
    result = lint_lib.scan(memory_dir, config)
    if not result["readable"]:
        return False, "memory dir is not readable"
    found = lint_lib.findings(result, config)
    chars = result["index_chars"]
    size = lint_lib.distill_size(result)
    threshold = lint_lib.distill_at_chars(config)
    ok = not found and size < threshold
    detail = "index %d chars, %d bytes, threshold %d" % (
        chars, result.get("index_bytes") or 0, threshold)
    if size >= threshold:
        detail += " (not under the threshold)"
    if found:
        detail += "; findings: %s" % "; ".join(str(f) for f in found)
    return ok, detail


def dispose(fixture_root, runs, cwds_created, keep):
    """Remove everything a proof created. Never raises."""
    runs = [r for r in (runs or []) if isinstance(r, dict)]
    for run in runs:
        try:
            cwd = run.get("cwd")
            if cwd:
                harness_lib._cleanup_scratchpad(
                    run, None, os.path.basename(harness_lib.projects_dir_for(cwd)))
        except Exception:
            pass
    for cwd in cwds_created or []:
        try:
            first = next((r for r in runs if r.get("cwd") == cwd), None)
            if first is not None and first.get("pdir_preexisting") is False:
                shutil.rmtree(harness_lib.projects_dir_for(cwd), ignore_errors=True)
        except Exception:
            pass
    for run in runs:
        grader = run.get("grader_cwd")
        if isinstance(grader, str) and grader:
            shutil.rmtree(grader, ignore_errors=True)
    if not keep and fixture_root:
        shutil.rmtree(fixture_root, ignore_errors=True)


# --- child runs ------------------------------------------------------------


def resolve_binary():
    """$CLAUDE_CODE_EXECPATH if executable, else `claude` on PATH, else None."""
    path = os.environ.get("CLAUDE_CODE_EXECPATH")
    if path and os.access(path, os.X_OK):
        return path
    return shutil.which("claude")


def _strip_paid(env):
    for name in PAID_AUTH_VARS:
        env.pop(name, None)
    # Every provider selector, including ones a newer binary adds.
    for name in [n for n in env if n.startswith("CLAUDE_CODE_USE_")]:
        del env[name]
    return env


def child_env(fixture, base_env):
    env = dict(base_env)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
    _strip_paid(env)
    env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(fixture.proof, "config.json")
    env["CLAUDE_TOOL_RULES_STATE_DIR"] = os.path.join(fixture.proof, "state")
    return env


def grader_env(base_env):
    env = dict(base_env)
    env.pop("CLAUDECODE", None)
    _strip_paid(env)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def child_argv(binary, prompt, memdir, extra):
    return ([binary, "-p", prompt, "--setting-sources", "project", "--settings",
             json.dumps({"autoMemoryDirectory": memdir}), "--strict-mcp-config",
             "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
            + list(extra))


def grader_argv(binary, prompt):
    return [binary, "-p", prompt, "--model", "opus", "--setting-sources", "project",
            "--strict-mcp-config", "--no-session-persistence", "--output-format",
            "stream-json", "--verbose", "--tools", ""]


def run_child(argv, cwd, env, timeout):
    """Run one child; any failure, a timeout included, is `exit: -1`."""
    model = argv[argv.index("--model") + 1] if "--model" in argv[:-1] else None
    record = {"cwd": cwd, "exit": -1, "init": None, "result_text": None,
              "window": None, "overage": False, "pdir_preexisting": False,
              "model": model, "api_key_source": None, "api_provider": None}
    try:
        record["pdir_preexisting"] = os.path.exists(harness_lib.projects_dir_for(cwd))
        done = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout)
        text = done.stdout.decode("utf-8", errors="replace")
        init, result, _tool_uses = harness_lib._parse_stream_json(text)
        window, overage = None, False
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
                continue
            info = event.get("rate_limit_info")
            info = info if isinstance(info, dict) else {}
            windows = info.get("unifiedWindows")
            window = windows if isinstance(windows, dict) else None
            if info.get("isUsingOverage"):
                overage = True
        reply = result.get("result") if isinstance(result, dict) else None
        record.update({"init": init, "result_text": reply if isinstance(reply, str) else None,
                       "window": window, "overage": overage, "exit": done.returncode})
        if isinstance(init, dict):
            record["api_key_source"] = init.get("apiKeySource")
            record["api_provider"] = init.get("apiProvider")
    except Exception:
        record["exit"] = -1
    return record


def must_stop(record):
    """A reason to halt the proof, or None. Fails closed on a partial reading."""
    if record.get("overage"):
        return "overage flipped on"
    window = record.get("window")
    five = window.get("five_hour") if isinstance(window, dict) else None
    used = five.get("utilization") if isinstance(five, dict) else None
    if (isinstance(used, bool) or not isinstance(used, (int, float))
            or not math.isfinite(used) or not 0 <= used <= 1):
        return "usage unreadable"
    if used >= STOP_AT:
        return "5h window at %d%%" % int(round(used * 100))
    return None


def paid_auth(record):
    """A reason when the run was billed outside the subscription, or None.

    `apiKeySource` is "none" for a subscription session (binary 2.1.312) and
    "/login managed key" is a Console key; a missing value is not evidence.
    """
    provider = record.get("api_provider")
    if provider not in (None, "firstParty"):
        return "apiProvider is %r" % (provider,)
    source = record.get("api_key_source")
    if source not in (None, "none"):
        return "apiKeySource is %r" % (source,)
    return None


def unusable(record):
    if record.get("exit") != 0:
        return "child exited %r" % (record.get("exit"),)
    if record.get("init") is None:
        return "child produced no init record"
    if record.get("result_text") is None:
        return "child produced no result text"
    return None


def grader_prompt(pass_if, reply, changed):
    files = "\n".join("=== %s ===\n%s" % (rel, text) for rel, text in changed.items())
    return ("You are grading one run of a task against a criterion written before the run.\n"
            "Criterion: %s\n\n"
            "The task's final reply:\n<<<\n%s\n>>>\n\n"
            "Files the task created or changed (path, then full text):\n%s\n\n"
            "Reply with ONLY a JSON object: "
            '{"verdict": "pass" or "fail", "why": "<one sentence>"}'
            % (pass_if, reply or "", files or "(none)"))


def parse_verdict(text):
    """The grader's JSON as a dict, or None (G4)."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    lines = text.splitlines()
    if (len(lines) >= 2 and lines[0].strip() in ("```", "```json")
            and lines[-1].strip() == "```"):
        text = "\n".join(lines[1:-1])
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if (isinstance(data, dict) and set(data) == {"verdict", "why"}
            and data["verdict"] in ("pass", "fail") and isinstance(data["why"], str)):
        return data
    return None


# --- the proof -------------------------------------------------------------


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_json(path, data):
    directory = os.path.dirname(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Run(object):
    """Mutable state of one proof: checks, run records, the fixture."""

    def __init__(self, binary, runner, base_env):
        self.binary, self.runner, self.base_env = binary, runner, base_env
        self.checks, self.runs, self.grader_cwds = [], [], []
        self.fixture = None
        self._reported = 0

    def check(self, stage, subject, result, detail):
        fresh, self._reported = self.runs[self._reported:], len(self.runs)
        runs = []
        for record in fresh:
            window = record.get("window")
            five = window.get("five_hour") if isinstance(window, dict) else None
            used = five.get("utilization") if isinstance(five, dict) else None
            runs.append({"model": record.get("model"),
                         "api_key_source": record.get("api_key_source"),
                         "api_provider": record.get("api_provider"), "five_hour": used})
        self.checks.append({"stage": stage, "subject": subject, "result": result,
                            "detail": detail, "runs": runs})
        return result

    def run(self, argv, cwd, env, timeout):
        # Recorded BEFORE the call: a run interrupted mid-flight (Terminated is a
        # BaseException) must still leave dispose() what it needs to remove a
        # projects dir that run created.
        index = len(self.runs)
        self.runs.append({"cwd": cwd, "pdir_preexisting":
                          os.path.exists(harness_lib.projects_dir_for(cwd))})
        record = self.runner(argv, cwd, env, timeout)
        if isinstance(record, dict) and "pdir_preexisting" not in record:
            record = dict(record, pdir_preexisting=self.runs[index]["pdir_preexisting"])
        self.runs[index] = record
        return record

    def fault(self, record, watch_surface=True):
        """A reason the run cannot count (harness fault or budget), else None."""
        reason = unusable(record)
        if reason:
            return "harness fault: %s" % reason
        reason = paid_auth(record)
        if reason:
            return "paid auth: %s" % reason
        reason = must_stop(record)
        if reason:
            return "budget: %s" % reason
        if watch_surface and surface_hashes(self.fixture.root) != self.fixture.surface:
            return "instruction surface changed during a run"
        return None


def _stage_negative(ctx):
    fx = ctx.fixture
    payload = {"session_id": "neg", "tool_name": "Read",
               "tool_input": {"file_path": os.path.join(fx.root, "x")},
               "hook_event_name": "PostToolUse"}
    command = production_posttooluse()["hooks"][0]["command"]
    try:
        done = subprocess.run(command, shell=True, input=json.dumps(payload),
                              env=child_env(fx, ctx.base_env), cwd=fx.root,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=15)
    except Exception as exc:
        return ctx.check("negative control", "production hook", "HALTED",
                         "could not run the production hook: %s" % exc)
    if done.stdout.strip():
        return ctx.check("negative control", "production hook", "FAIL",
                         "the hook emitted output for a Read: %s" % done.stdout.strip()[:200])
    return ctx.check("negative control", "production hook", "PASS",
                     "the hook stays silent on a non-matching call")


def _emitted_keys(state_dir):
    keys = []
    if os.path.isdir(state_dir):
        for name in sorted(os.listdir(state_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(state_dir, name), encoding="utf-8") as fh:
                    emitted = json.load(fh).get("emitted", [])
            except (OSError, ValueError, AttributeError):
                continue
            keys += [k for k in emitted if isinstance(k, str)]
    return keys


def _clear_logs(fx):
    for name in ("loaded.jsonl", "blocked.jsonl"):
        try:
            os.remove(os.path.join(fx.proof, name))
        except OSError:
            pass


def _fail(ctx, stage, subject, detail):
    """FAIL, unless containment blocked a call: then it is a harness fault (P5).

    Mirrors harness-check's VOID: a model typing a variant of the probe is not
    evidence that the page failed to load.
    """
    try:
        with open(os.path.join(ctx.fixture.proof, "blocked.jsonl"), encoding="utf-8") as fh:
            first = next((line.strip() for line in fh if line.strip()), None)
    except OSError:
        first = None
    if first is not None:
        return ctx.check(stage, subject, "HALTED",
                         "harness fault: the child's call was blocked by containment: %s"
                         % first[:200])
    return ctx.check(stage, subject, "FAIL", detail)


def _stage_tool(ctx, page, abs_path):
    fx = ctx.fixture
    subject = page["file"]
    command = _tool_command(page)
    _clear_logs(fx)
    state_dir = os.path.join(fx.proof, "state")
    if os.path.isdir(state_dir):
        for name in os.listdir(state_dir):
            try:
                os.remove(os.path.join(state_dir, name))
            except OSError:
                pass
    prompt = "Run `%s`, then list every fixture check token you can see." % command
    extra = ["--model", "haiku", "--tools", "Bash", "--allowedTools",
             "Bash(%s)" % command, "--max-turns", "4"]
    record = ctx.run(child_argv(ctx.binary, prompt, fx.mem, extra), fx.root,
                     child_env(fx, ctx.base_env), HAIKU_TIMEOUT)
    reason = ctx.fault(record)
    if reason:
        return ctx.check("tool canary", subject, "HALTED", reason)
    token = fx.tokens[page["file"]]
    if token not in record["result_text"]:
        return _fail(ctx, "tool canary", subject,
                     "the page's token was not in the reply")
    suffix = "|" + os.path.realpath(abs_path)
    if not any(k.endswith(suffix) for k in _emitted_keys(state_dir)):
        return _fail(ctx, "tool canary", subject,
                     "token seen but the hook never recorded delivering the page")
    return ctx.check("tool canary", subject, "PASS",
                     "token in the reply and the hook recorded the delivery")


def _stage_path(ctx, page, abs_path):
    fx = ctx.fixture
    subject = page["file"]
    _clear_logs(fx)
    target = page["probe"]["path"]
    extra = ["--model", "haiku", "--tools", "Read", "--max-turns", "4"]
    record = ctx.run(child_argv(ctx.binary, "Read %s, then reply OK." % target, fx.mem, extra),
                     fx.root, child_env(fx, ctx.base_env), HAIKU_TIMEOUT)
    reason = ctx.fault(record)
    if reason:
        return ctx.check("path canary", subject, "HALTED", reason)
    entries = []
    try:
        with open(os.path.join(fx.proof, "loaded.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("file_path"), str):
                    entries.append(entry)
    except OSError:
        pass
    want = os.path.realpath(abs_path)
    reasons = [e.get("load_reason") for e in entries
               if os.path.realpath(e["file_path"]) == want]
    if "session_start" in reasons:
        return _fail(ctx, "path canary", subject,
                     "the page was loaded at session start, so a path match proves nothing")
    if "path_glob_match" not in reasons:
        return _fail(ctx, "path canary", subject,
                     "no path_glob_match load for the page when %s was read" % target)
    return ctx.check("path canary", subject, "PASS", "loaded on a path_glob_match only")


def _stage_behaviour(ctx, case):
    fx = ctx.fixture
    subject = case["memory"]
    before = snapshot(fx.root)
    for rel, text in case["files"].items():
        _write(str(distill_lib.resolve_inside(fx.root, rel)), text)
    seeded = snapshot(fx.root)
    extra = ["--model", "opus", "--tools", "Read,Glob,Grep,Edit,Write",
             "--permission-mode", "acceptEdits", "--max-turns", "20"]
    record = ctx.run(child_argv(ctx.binary, case["prompt"], fx.mem, extra), fx.root,
                     child_env(fx, ctx.base_env), OPUS_TIMEOUT)
    reason = ctx.fault(record)
    if reason:
        return ctx.check("behaviour", subject, "HALTED", reason)
    changed = changed_files(fx.root, seeded)
    # Beside the fixture, not inside it: still fresh and empty, and in the one
    # root sweep_orphans covers.
    gcwd = tempfile.mkdtemp(prefix="index-distill-grader-", dir=os.path.dirname(fx.root))
    ctx.grader_cwds.append(gcwd)
    prompt = grader_prompt(case["pass_if"], record["result_text"], changed)
    grade = ctx.run(grader_argv(ctx.binary, prompt), gcwd, grader_env(ctx.base_env),
                    OPUS_TIMEOUT)
    grade["grader_cwd"] = gcwd
    reason = ctx.fault(grade, watch_surface=False)
    if reason:
        return ctx.check("behaviour", subject, "HALTED", "grader: %s" % reason)
    verdict = parse_verdict(grade["result_text"])
    if verdict is None:
        return ctx.check("behaviour", subject, "HALTED",
                         "the grader's reply was not the expected JSON")
    restore(fx.root, before)
    if verdict["verdict"] == "fail":
        return ctx.check("behaviour", subject, "FAIL", verdict["why"])
    return ctx.check("behaviour", subject, "PASS", verdict["why"])


def _stages(ctx, manifest, config):
    """Run the stages cheapest-first; return the first non-PASS result, else PASS (G2)."""
    fx = ctx.fixture
    ok, detail = structural(fx.mem, config)
    if ctx.check("structural", "index", "PASS" if ok else "FAIL", detail) != "PASS":
        return "FAIL"
    result = _stage_negative(ctx)
    if result != "PASS":
        return result
    for page, abs_path in fx.pages:
        if page["bucket"] == "tool":
            result = _stage_tool(ctx, page, abs_path)
            if result != "PASS":
                return result
    for page, abs_path in fx.pages:
        if page["bucket"] == "path":
            result = _stage_path(ctx, page, abs_path)
            if result != "PASS":
                return result
    for case in manifest["behaviour"]:
        result = _stage_behaviour(ctx, case)
        if result != "PASS":
            return result
    return "PASS"


_EXIT = {"PASS": 0, "FAIL": 1, "HALTED": 3}


def prove(manifest_path, memory_dir, workspace, session_id, config=None, config_doc=None,
          runner=None, binary=None, keep=False, today=None, rng=None, base_env=None):
    """Steps 1-10 of design 2.5. Returns the CLI exit code."""
    holder = distill_claim.read_live()
    if not session_id or not holder or holder.get("session_id") != session_id:
        print("the distill claim is not held by this session — run "
              "claude-index-distill claim first")
        return 1
    # The claim admits the same session twice, so it cannot keep a second prove
    # from sweeping a running one's fixture. The lock can, and the OS drops it
    # when its holder dies, so a killed run never leaves it stuck.
    with contextlib.ExitStack() as stack:
        try:
            base = fixture_base(config)
            held = stack.enter_context(portable.exclusive_lock(os.path.join(base, PROVE_LOCK)))
        except OSError:
            pass  # no usable fixture base: _prove halts on it, as it always has
        else:
            if not held:
                print("another prove is running against %s — wait for it to finish, "
                      "then rerun" % base)
                return 1
            removed, failed = sweep_orphans(base)
            for path in removed:
                print("removed a proof dir an interrupted earlier run left behind: %s" % path)
            for path in failed:
                print("could NOT remove a leftover proof dir (it may hold a copy of memory): %s"
                      % path)
        return _prove(manifest_path, memory_dir, workspace, config, config_doc, runner,
                      binary, keep, today, rng, base_env)


def _prove(manifest_path, memory_dir, workspace, config, config_doc, runner, binary, keep,
           today, rng, base_env):
    try:
        with open(manifest_path, "rb") as fh:
            manifest_raw = fh.read()
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    except OSError as exc:
        print("- cannot read the manifest: %s" % exc)
        return 1
    proof_path = manifest_path + ".proof.json"
    # G3: from here on no earlier PASS can survive a rerun that does not finish.
    _atomic_json(proof_path, {"verdict": "INCOMPLETE", "manifest_sha256": manifest_sha,
                              "proved_at": _now()})

    config_doc = toolkit_config.load() if config_doc is None else config_doc
    today = today or datetime.date.today().isoformat()
    rng = rng or random.Random()
    base_env = dict(os.environ if base_env is None else base_env)
    ctx = _Run(None, runner or run_child, base_env)
    deps, verdict, fixture_root = {}, "FAIL", None
    try:
        try:
            # Parse the bytes that were hashed: the file is never reopened.
            manifest = distill_lib.parse_manifest(manifest_raw)
            problems = distill_lib.validate(manifest, memory_dir, workspace, config)
        except (distill_lib.ManifestError, OSError) as exc:
            problems = [str(exc)]
        if problems:
            for problem in problems:
                print("- %s" % problem)
        else:
            ctx.binary = binary or resolve_binary()
            if ctx.binary is None:
                print("proof cannot run here: no claude binary "
                      "($CLAUDE_CODE_EXECPATH or PATH)")
                verdict = "HALTED"
            else:
                try:
                    # Live roots, before the fixture exists: a live change during
                    # the proof makes `apply` refuse.
                    deps = distill_lib.dependency_set(manifest, memory_dir, workspace)
                    ctx.fixture = build_fixture(manifest, memory_dir, workspace, config,
                                                config_doc, today, fixture_base(config), rng)
                    fixture_root = ctx.fixture.root
                except BaseException as exc:
                    fixture_root = getattr(exc, "root", None)
                    if not isinstance(exc, (FixtureError, OSError, distill_lib.PathError,
                                            KeyError, TypeError)):
                        raise
                    print("proof cannot run here: %s" % exc)
                    verdict = "HALTED"
                else:
                    verdict = _stages(ctx, manifest, config)
        _atomic_json(proof_path, {
            "verdict": verdict, "manifest_sha256": manifest_sha,
            "roots": {"memory_dir": os.path.realpath(memory_dir),
                      "workspace": os.path.realpath(workspace)},
            "dependencies": json.loads(json.dumps(deps)),
            "checks": ctx.checks, "proved_at": _now()})
        for c in ctx.checks:
            tags = "".join(" [%s key=%s provider=%s 5h=%s]" % (
                r["model"], r["api_key_source"], r["api_provider"], r["five_hour"])
                for r in c["runs"])
            print("%s: %s %s — %s%s" % (c["stage"], c["subject"], c["result"],
                                        c["detail"], tags))
        print("verdict: %s" % verdict)
        print("proof: %s" % proof_path)
        if verdict == "PASS":
            print("Behaviour checks are one run each, graded by a model: "
                  "a PASS is evidence, not a guarantee.")
        return _EXIT[verdict]
    finally:
        dispose(fixture_root, ctx.runs,
                ([fixture_root] if fixture_root else []) + list(ctx.grader_cwds), keep)
        for gcwd in ctx.grader_cwds:
            shutil.rmtree(gcwd, ignore_errors=True)
        if keep and fixture_root:
            print("fixture kept at %s" % fixture_root)
