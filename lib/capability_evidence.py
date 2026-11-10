"""Run each capability's test suites and record what actually happened.

The silent failure this guards: a capability reads as tested because its suite
file exists, or because the suite exited 0 having run zero tests, or having
skipped everything. Nothing else complains. So a recorder (a sitecustomize.py
the runner puts on PYTHONPATH) writes every test outcome to a file keyed by the
suite process's pid, and `judge` calls a capability evidenced only when, under
every interpreter that ran, every suite finished, at least one test passed,
nothing failed, and every skip is on the allowlist.

Pure functions take explicit inputs so a positive control can feed `judge`
records with one defect; `run_suite` is the thin subprocess layer.
"""
import ast
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile

SUITE_ROOTS = ("tools", "lib", "skills", "evals", "scripts")
# Skills that are instructions only and have no suite: their live record is their
# whole evidence. A literal, not read from the registry, so deleting another
# skill's `tests` cannot quietly move it onto the live record alone.
LIVE_ONLY_SKILLS = frozenset({"skill:caffeinate", "skill:claude-md-distill", "skill:handover",
                              "skill:memory-distill", "skill:wrap-up"})
ENV_ALLOW = ("USER", "LOGNAME", "LANG", "TMPDIR", "TERM")
LIVE_KINDS = ("positive", "out-of-scope", "reference")
LIVE_SCHEMA = 2
# completed: did the job. gate-stop: stopped at the skill's own decision gate, a pass
# that certifies stopping only. env-blocked: a permission or environment block,
# never a pass, whatever `observed` says.
OUTCOMES = ("completed", "gate-stop", "env-blocked")
GATE_STOP_NOTE = "positive case stopped at a decision gate: certifies stopping only"
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
CLOSURE_SKIP_DIRS = ("tests", "__pycache__")
# refcheck's reference forms. A `claude-*` token counts only when it names a file
# under tools/*/, so prose like "claude-code" adds nothing.
PLUGIN_REF = re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?/([A-Za-z0-9_./-]+)")
SKILL_DIR_REF = re.compile(r"\$\{?CLAUDE_SKILL_DIR\}?/([A-Za-z0-9_./-]+)")
LOCAL_REF = re.compile(r"(?<![A-Za-z0-9_/.-])((?:references|scripts)/[A-Za-z0-9_./-]+)")
TOOL_REF = re.compile(r"(?<![A-Za-z0-9_.-])(claude-[a-z0-9-]+)")
RECORD_NAME = re.compile(r"^(\d+)-(\d+)\.json$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+$")

# Written as sitecustomize.py. Inert unless CAPABILITY_EVIDENCE_DIR is set. The
# subclass calls super() in every hook so unittest's own output is unchanged.
RECORDER_SOURCE = '''\
import json
import os
import unittest

_DIR = os.environ.get("CAPABILITY_EVIDENCE_DIR")
_SEQ = [0]

if _DIR:
    class _Recorder(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._evidence = []

        def _note(self, test, outcome, reason=""):
            self._evidence.append({"id": test.id(), "outcome": outcome, "reason": str(reason)})

        def addSuccess(self, test):
            super().addSuccess(test)
            self._note(test, "pass")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._note(test, "fail")

        def addError(self, test, err):
            super().addError(test, err)
            self._note(test, "error")

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._note(test, "skip", reason)

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self._note(test, "xfail")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._note(test, "xpass")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                failed = issubclass(err[0], test.failureException)
                self._note(test, "fail" if failed else "error")

        def stopTestRun(self):
            super().stopTestRun()
            seq = _SEQ[0]
            _SEQ[0] += 1
            pid = os.getpid()
            final = os.path.join(_DIR, "%d-%d.json" % (pid, seq))
            with open(final + ".tmp", "w", encoding="utf-8") as handle:
                json.dump({"pid": pid, "seq": seq, "tests": self._evidence}, handle)
            os.replace(final + ".tmp", final)

    unittest.TextTestRunner.resultclass = _Recorder
'''


def write_recorder(dirpath):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "sitecustomize.py"), "w", encoding="utf-8") as handle:
        handle.write(RECORDER_SOURCE)


def _is_suite_file(path):
    return (fnmatch.fnmatch(os.path.basename(path), "test_*.py")
            and os.path.isfile(path) and not os.path.islink(path))


def _walk_suites(repo, start):
    """Test files sitting directly in a directory named tests, under start."""
    found = []
    for dirpath, _dirs, files in os.walk(os.path.join(repo, start)):
        if os.path.basename(dirpath) != "tests":
            continue
        for name in files:
            full = os.path.join(dirpath, name)
            if _is_suite_file(full):
                found.append(os.path.relpath(full, repo).replace(os.sep, "/"))
    return found


def suites_for(repo, rel):
    full = os.path.join(repo, rel)
    if os.path.isfile(full):
        return [rel] if _is_suite_file(full) else []
    if os.path.isdir(full):
        return sorted(_walk_suites(repo, rel))
    return []


def all_suites(repo):
    found = []
    for top in SUITE_ROOTS:
        found.extend(_walk_suites(repo, top))
    return sorted(found)


def resolve_interpreter(version):
    """Resolved like ci/local-checks.sh: uv's system python, else pythonV on PATH."""
    path = None
    if shutil.which("uv"):
        result = subprocess.run(["uv", "python", "find", "--system", version],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8")
        if result.returncode == 0:
            path = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
    if not path:
        path = shutil.which("python" + version)
    if path and os.access(path, os.X_OK):
        return path
    return None


def _version_output(argv):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout + result.stderr).strip()


def interpreter_version(path):
    return _version_output([path, "--version"]) or ""


def node_version():
    return _version_output(["node", "--version"]) or None


def _refuse_tmp_root(root):
    real = os.path.realpath(root)
    banned = {os.path.realpath(p) for p in ("/tmp", "/private/tmp")}
    if os.environ.get("TMPDIR"):
        banned.add(os.path.realpath(os.environ["TMPDIR"]))
    for bad in banned:
        if real == bad or real.startswith(bad + os.sep):
            raise ValueError("root %s is under %s; use a directory under the real ~/.cache"
                             % (root, bad))


def _component_and_argv(suite):
    parts = suite.split("/")
    if "tests" not in parts[:-1]:
        raise ValueError("suite %s is not under a tests/ directory" % suite)
    at = parts.index("tests")
    return "/".join(parts[:at]), "/".join(parts[at:])


def _read_record(evidence_dir, pid):
    """Tests of the highest-seq record file written by process pid, else None."""
    mine = []
    for name in os.listdir(evidence_dir):
        match = RECORD_NAME.match(name)
        if match and int(match.group(1)) == pid:
            mine.append((int(match.group(2)), name))
    if not mine:
        return None
    try:
        with open(os.path.join(evidence_dir, max(mine)[1]), encoding="utf-8") as handle:
            data = json.load(handle)
        if data["pid"] != pid or not isinstance(data["tests"], list):
            return None
        return data["tests"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def run_suite(repo, suite, python, version, root, timeout=1200, recorder=True):
    _refuse_tmp_root(root)
    component, argv_path = _component_and_argv(suite)
    os.makedirs(root, exist_ok=True)
    work = tempfile.mkdtemp(prefix="suite-", dir=root)
    home = os.path.join(work, "home")
    shim = os.path.join(work, "shim")
    evidence = os.path.join(work, "evidence")
    for path in (home, shim, evidence):
        os.makedirs(path)
    os.symlink(python, os.path.join(shim, "python3"))
    env = {"HOME": home, "PYTHONUNBUFFERED": "1",
           "PATH": shim + os.pathsep + os.environ.get("PATH", "")}
    for key in ENV_ALLOW:
        if key in os.environ:
            env[key] = os.environ[key]
    if recorder:
        write_recorder(os.path.join(work, "recorder"))
        env["PYTHONPATH"] = os.path.join(work, "recorder")
        env["CAPABILITY_EVIDENCE_DIR"] = evidence
    log = os.path.join(work, "suite.log")
    timed_out = False
    with open(log, "wb") as out:
        proc = subprocess.Popen([python, argv_path], cwd=os.path.join(repo, component),
                                env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            rc = proc.wait()
    return {"suite": suite, "version": version, "rc": rc, "timed_out": timed_out,
            "record": _read_record(evidence, proc.pid), "log": log}


def run_all(repo, suites, interpreters, root, jobs, timeout):
    work = [(suite, version, path) for suite in suites
            for version, path in sorted(interpreters.items())]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(run_suite, repo, suite, path, version, root, timeout)
                   for suite, version, path in work]
        return [future.result() for future in futures]


def load_allowlist(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("schema") != 1 \
            or not isinstance(data.get("skips"), list):
        raise ValueError("%s: expected {\"schema\": 1, \"skips\": [...]}" % path)
    for index, entry in enumerate(data["skips"]):
        label = "%s skips[%d]" % (path, index)
        if not isinstance(entry, dict):
            raise ValueError("%s: not an object" % label)
        label += " (%s::%s)" % (entry.get("suite"), entry.get("test"))
        if set(entry) != {"suite", "test", "python", "reason"}:
            raise ValueError("%s: keys must be exactly suite, test, python, reason" % label)
        for key in entry:
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError("%s: %s must be a non-empty string" % (label, key))
        if entry["python"] != "any" and not VERSION_RE.match(entry["python"]):
            raise ValueError("%s: python must be 'any' or a version like 3.9" % label)
    return data["skips"]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_files(repo, rel):
    """rel itself if a file, else every file under it, minus tests/ and caches."""
    full = os.path.join(repo, rel)
    if os.path.isfile(full):
        return [rel]
    found = []
    for dirpath, dirnames, filenames in os.walk(full, followlinks=True):
        dirnames[:] = sorted(d for d in dirnames if d not in CLOSURE_SKIP_DIRS)
        for name in sorted(filenames):
            found.append(os.path.relpath(os.path.join(dirpath, name), repo))
    return found


def _inside(repo, path):
    """repo-relative path of `path`'s real target, or None when it leaves the repo
    or does not exist."""
    real_repo = os.path.realpath(repo)
    real = os.path.realpath(path)
    if not os.path.exists(real) or not (real == real_repo or real.startswith(real_repo + os.sep)):
        return None
    return os.path.relpath(real, real_repo)


def _plugin_dirs(repo, skill_md):
    """Plugin directories that ship this SKILL.md (realpath match)."""
    target = os.path.realpath(os.path.join(repo, skill_md))
    name = os.path.basename(os.path.dirname(target))
    dirs = []
    candidates = [d for d in os.listdir(repo) if d.startswith("plugin")]
    if os.path.isdir(os.path.join(repo, "plugins")):
        candidates += [os.path.join("plugins", d) for d in os.listdir(os.path.join(repo, "plugins"))]
    for rel in sorted(candidates):
        if not os.path.isfile(os.path.join(repo, rel, ".claude-plugin", "plugin.json")):
            continue
        shipped = os.path.join(repo, rel, "skills", name, "SKILL.md")
        if os.path.isfile(shipped) and os.path.realpath(shipped) == target:
            dirs.append(rel)
    return dirs


def _is_python(repo, rel):
    if rel.endswith(".py"):
        return True
    try:
        with open(os.path.join(repo, rel), "rb") as handle:
            first = handle.readline()
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def _imported_files(repo, rel, text):
    """Repo files that `rel`'s imports resolve to: its own dir, lib/, tools/*/."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module.split(".")[0])
    found = []
    tools = os.path.join(repo, "tools")
    tool_dirs = sorted(os.path.join("tools", d) for d in os.listdir(tools)) \
        if os.path.isdir(tools) else []
    # Python's own order: the file's dir, then lib/; another tool's dir only when
    # neither has the module (usage-dispatch imports build-cost's lib that way).
    for name in sorted(names):
        for group in ([os.path.dirname(rel)], ["lib"], tool_dirs):
            hits = [_inside(repo, os.path.join(repo, d, name + ".py")) for d in group]
            hits = [h for h in hits if h and "tests" not in h.split(os.sep)]
            if hits:
                found += hits
                break
    return found


def _tool_files(repo, token):
    tools = os.path.join(repo, "tools")
    if not os.path.isdir(tools):
        return []
    hits = []
    for tool in sorted(os.listdir(tools)):
        hit = _inside(repo, os.path.join(tools, tool, token))
        if hit and os.path.isfile(os.path.join(repo, hit)):
            hits.append(hit)
    return hits


def skill_closure(repo, cap, caps):
    """Every repo file a live run of skill `cap` executes or reads as instructions,
    derived from the tree itself (row 56a): the skill folder, the plugin manifest
    that ships it, every path its text names, its non-skill `requires`, and the
    helpers those import or name, until nothing new is added. Required skills
    are not followed: each has its own live record."""
    if not cap.get("entry_points"):
        raise ValueError("%s has no entry point" % cap["id"])
    entry = _inside(repo, os.path.join(repo, cap["entry_points"][0]))
    if entry is None:
        raise ValueError("%s: entry point %s does not exist" % (cap["id"], cap["entry_points"][0]))
    # The real folder: closure members are real paths, so an entry point spelled
    # through a plugin's skills/ symlink must still count its files as instructions.
    skill_dir = os.path.dirname(entry)
    plugins = _plugin_dirs(repo, entry)
    pending = list(_walk_files(repo, skill_dir))
    pending += [os.path.join(p, ".claude-plugin", "plugin.json") for p in plugins]
    for ident in cap.get("requires", []):
        required = caps.get(ident)
        if required is None or required["kind"] == "skill":
            continue
        for rel in required.get("entry_points", []):
            if os.path.exists(os.path.join(repo, rel)):
                pending += _walk_files(repo, rel)

    closure = set()
    while pending:
        rel = os.path.normpath(pending.pop())
        real = _inside(repo, os.path.join(repo, rel))
        if real is None or real in closure:
            continue
        closure.add(real)
        with open(os.path.join(repo, real), "rb") as handle:
            text = handle.read().decode("utf-8", "ignore")
        named = []
        # Paths and `claude-*` tools named in the skill's own instruction files.
        # A helper module that lists tool names (an install manifest, say) does not
        # run them, so its text is not scanned; its imports are.
        instructions = real == skill_dir or real.startswith(skill_dir + os.sep)
        for ref in PLUGIN_REF.findall(text) if instructions else ():
            named += [os.path.join(p, ref.rstrip(".")) for p in plugins]
        named += [os.path.join(skill_dir, ref.rstrip("."))
                  for ref in (SKILL_DIR_REF.findall(text) if instructions else ())]
        for ref in LOCAL_REF.findall(text) if instructions else ():
            ref = ref.rstrip(".")
            local = os.path.join(skill_dir, ref)
            named.append(local if os.path.exists(os.path.join(repo, local)) else ref)
        for token in set(TOOL_REF.findall(text)) if instructions else ():
            named += _tool_files(repo, token)
        here = os.path.dirname(real)
        for sibling in os.listdir(os.path.join(repo, here)):
            if sibling != os.path.basename(real) and sibling in text \
                    and os.path.isfile(os.path.join(repo, here, sibling)):
                named.append(os.path.join(here, sibling))
        if _is_python(repo, real):
            named += _imported_files(repo, real, text)
        for rel_named in named:
            if os.path.exists(os.path.join(repo, rel_named)):
                pending += _walk_files(repo, os.path.normpath(rel_named))
    return sorted(closure)


def skill_identity(repo, cap, caps):
    files = skill_closure(repo, cap, caps)
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.encode("utf-8") + b"\0")
        with open(os.path.join(repo, rel), "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\0")
    return {"closure_sha256": digest.hexdigest(), "files": files}


def load_live(path, registry_ids):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict) and data.get("schema") == 1:
        raise ValueError("%s: schema 1 receipts carry no implementation identity; "
                         "regenerate them as schema %d" % (path, LIVE_SCHEMA))
    if not isinstance(data, dict) or data.get("schema") != LIVE_SCHEMA \
            or not isinstance(data.get("claude_version"), str) or not data["claude_version"] \
            or not isinstance(data.get("records"), list):
        raise ValueError("%s: expected schema %d with claude_version and records"
                         % (path, LIVE_SCHEMA))
    base = os.path.dirname(os.path.abspath(path))
    by_id, errors = {}, []
    for record in data["records"]:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) \
                or not isinstance(record.get("cases"), list):
            raise ValueError("%s: each record needs a string id and a cases list" % path)
        ident = record["id"]
        identity = record.get("identity")
        if not isinstance(identity, dict) or not isinstance(identity.get("closure_sha256"), str) \
                or not HEX64.match(identity["closure_sha256"]):
            raise ValueError("%s: %s needs identity.closure_sha256 (64 hex)" % (path, ident))
        problems, notes = [], []
        if ident not in set(registry_ids):
            errors.append("live record for %s, which is not in the registry" % ident)
        if ident in by_id:
            errors.append("duplicate live record for %s" % ident)
        kinds = set()
        for case in record["cases"]:
            if not isinstance(case, dict) or case.get("kind") not in LIVE_KINDS \
                    or case.get("observed") not in ("pass", "fail") \
                    or not isinstance(case.get("transcript"), str) \
                    or not isinstance(case.get("sha256"), str) \
                    or ("outcome" in case and case["outcome"] not in OUTCOMES) \
                    or (case["kind"] == "positive" and "outcome" not in case):
                raise ValueError("%s: %s has a malformed case (a positive case needs an "
                                 "outcome: %s)" % (path, ident, ", ".join(OUTCOMES)))
            kinds.add(case["kind"])
            if case.get("outcome") == "env-blocked":
                problems.append("%s case environment-blocked: a permission or environment "
                                "block is not a pass" % case["kind"])
            elif case["observed"] != "pass":
                problems.append("%s case observed fail" % case["kind"])
            elif case.get("outcome") == "gate-stop" and case["kind"] == "positive":
                notes.append(GATE_STOP_NOTE)
            transcript = case["transcript"]
            full = os.path.join(base, transcript)
            if os.path.isabs(transcript) or not os.path.isfile(full):
                problems.append("%s transcript %s is missing" % (case["kind"], transcript))
            elif _sha256(full) != case["sha256"].lower():
                problems.append("%s transcript %s does not match its sha256"
                                % (case["kind"], transcript))
        for kind in LIVE_KINDS:
            if kind not in kinds:
                problems.append("no %s case" % kind)
        by_id[ident] = {"problems": problems, "notes": notes,
                        "identity": identity["closure_sha256"].lower()}
    return {"by_id": by_id, "errors": errors}


def _test_key(test_id):
    """A script-mode id without its `__main__.` module, including a class-level
    holder's `setUpClass (__main__.T)`, so allowlist keys read `T.test_x`."""
    return re.sub(r"(^|\()__main__\.", r"\1", test_id)


def _entry_applies(entry, version):
    return entry["python"] in ("any", version)


def git_state(repo):
    def git(*args):
        result = subprocess.run(["git", "-C", repo] + list(args), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (" ".join(args), result.stderr.strip()))
        return result.stdout
    return (git("rev-parse", "HEAD").strip(), git("rev-parse", "HEAD^{tree}").strip(),
            git("status", "--porcelain"))


def _judge_suite_version(suite, version, result, allowlist, reasons):
    """Judge one suite run; returns (tests passed, whether every test it ran was
    an allowlisted skip)."""
    if result is None:
        reasons.append("no result for suite %s on python %s" % (suite, version))
        return 0, False
    if result["timed_out"]:
        reasons.append("suite %s timed out on python %s" % (suite, version))
        return 0, False
    if result["rc"] != 0:
        reasons.append("suite %s exited %s on python %s" % (suite, result["rc"], version))
    if result["record"] is None:
        if result["rc"] == 0:
            reasons.append("recorder did not load for suite %s on python %s" % (suite, version))
        return 0, False
    passed = 0
    listed_skips = 0
    for test in result["record"]:
        key = _test_key(str(test.get("id")))
        outcome = test.get("outcome")
        label = "%s::%s on python %s" % (suite, key, version)
        if outcome == "pass":
            passed += 1
        elif outcome == "skip":
            listed = any(e["suite"] == suite and e["test"] == key and _entry_applies(e, version)
                         for e in allowlist)
            if listed:
                listed_skips += 1
            else:
                reasons.append("unlisted skip %s (%s)" % (label, test.get("reason", "")))
        elif outcome == "xfail":
            pass
        else:
            reasons.append("%s: %s" % (label, {"fail": "test failed", "error": "test errored",
                                               "xpass": "unexpected success"}.get(
                                                   outcome, "unknown outcome %r" % outcome)))
    record = result["record"]
    return passed, bool(record) and listed_skips == len(record)


def judge(registry, results, allowlist, live, selected_ids, check_unowned, repo):
    caps = {c["id"]: c for c in registry["capabilities"]}
    by_key = {(r["suite"], r["version"]): r for r in results}
    versions = sorted({r["version"] for r in results})
    owned = {}
    for ident, cap in caps.items():
        owned[ident] = {s for rel in cap.get("tests", []) for s in suites_for(repo, rel)}
    all_owned = set().union(*owned.values()) if owned else set()
    selected = list(caps) if selected_ids is None else list(selected_ids)

    out = {"capabilities": {}, "global": [], "unowned": [], "stale": []}
    for ident in selected:
        if ident not in caps:
            out["global"].append("selected id %s is not in the registry" % ident)
    for error in live.get("errors", []):
        out["global"].append("live evidence: " + error)
    for ident in live.get("by_id", {}):
        if ident in caps and caps[ident]["kind"] != "skill":
            out["global"].append("live record for %s, which is not a skill" % ident)

    run_suites = {r["suite"] for r in results}
    cap_reasons = {}
    for ident in selected:
        if ident not in caps:
            continue
        reasons = []
        cap_reasons[ident] = reasons
        suites = sorted(owned[ident])
        for rel in caps[ident].get("tests", []):
            if not suites_for(repo, rel):
                reasons.append("tests path %s matches no suite" % rel)
        # Only a declared instructions-only skill may list no suite (its live record
        # is its evidence); anything else with no suite has nothing that ran.
        if not suites and ident not in LIVE_ONLY_SKILLS:
            reasons.append("no test passed: capability has no suite")
        # Per suite, not summed: one real suite must not cover for a sibling that
        # ran nothing. The one exception is a suite whose every test is an
        # allowlisted skip (an opt-in live suite, say): explicit, with a reason,
        # and the capability as a whole still needs a passing test.
        for version in versions:
            total = 0
            for suite in suites:
                passed, all_listed = _judge_suite_version(
                    suite, version, by_key.get((suite, version)), allowlist, reasons)
                total += passed
                if passed < 1 and not all_listed:
                    reasons.append("no test passed in suite %s on python %s" % (suite, version))
            if suites and total < 1:
                reasons.append("no test passed on python %s" % version)
        if not versions and suites:
            reasons.append("no test passed: suites were not run")

    # Stale entries: a suite that ran must contain a test (any outcome) that each
    # applicable entry names, or the allowlist is growing permissive silently.
    seen = set()
    for result in results:
        if result["record"] is None:
            continue
        for test in result["record"]:
            key = _test_key(str(test.get("id")))
            for index, entry in enumerate(allowlist):
                if entry["suite"] == result["suite"] and entry["test"] == key \
                        and _entry_applies(entry, result["version"]):
                    seen.add(index)
    for index, entry in enumerate(allowlist):
        suite = entry["suite"]
        applicable = [v for v in versions if _entry_applies(entry, v)]
        if suite in run_suites and applicable and index not in seen:
            text = "stale skip entry %s::%s (python %s): no such test was recorded" \
                   % (suite, entry["test"], entry["python"])
            out["stale"].append(text)
            owners = [i for i in selected if i in caps and suite in owned[i]]
            for owner in owners:
                cap_reasons[owner].append(text)
            if check_unowned and not owners:
                out["global"].append(text)
        elif check_unowned and suite not in run_suites:
            out["global"].append("allowlist entry %s::%s names a suite that was not run"
                                 % (suite, entry["test"]))

    out["unowned"] = sorted(s for s in all_suites(repo) if s not in all_owned)
    if check_unowned and out["unowned"]:
        out["global"].append("%d suites are owned by no capability: %s"
                             % (len(out["unowned"]), ", ".join(out["unowned"])))

    for ident, reasons in cap_reasons.items():
        notes = []
        if not reasons and caps[ident]["kind"] == "skill":
            record = live.get("by_id", {}).get(ident)
            if record is None:
                out["capabilities"][ident] = {"state": "awaiting-live", "reasons": [
                    "tests evidenced; no live record"], "notes": []}
                continue
            problems = list(record["problems"])
            try:
                candidate = skill_identity(repo, caps[ident], caps)["closure_sha256"]
                if candidate != record["identity"]:
                    problems.append("implementation identity mismatch: receipt %s, candidate "
                                    "%s (the skill or what it runs changed since the live "
                                    "run)" % (record["identity"][:12], candidate[:12]))
            except (ValueError, OSError) as err:
                problems.append("cannot compute implementation identity: %s" % err)
            reasons = ["live record: " + p for p in problems]
            notes = list(record["notes"])
        out["capabilities"][ident] = {"state": "failed" if reasons else "evidenced",
                                      "reasons": reasons, "notes": notes}
    return out
