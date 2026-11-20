"""Per-profile readiness: is each SELECTED profile's plugin configured, installed,
or proven to run in this Claude Code session, and on which Claude Code version.
Stdlib only; importable by path like its siblings.

States, for each selected profile:
  configured     selected, not enabled in ~/.claude/settings.json
  installed      enabled, but this session's heartbeat for it is not proven
  runtime-proven heartbeat proven AND this session's Claude Code version equals
                 the registry's tested_claude_code
  unverified     heartbeat proven, but the session version is unknown or not the
                 tested one

The session version is read from this session's own transcript. `claude --version`
(path_version) is shown for information only and never makes anything proven: the
binary on PATH is not necessarily the one running this session.

CLI: readiness.py report <repo>   (session id from CLAUDE_CODE_SESSION_ID)
  exit 0 every selected profile is runtime-proven or unverified
       1 any selected profile is configured or installed
       2 the report cannot be computed (message on stderr)
"""

import glob
import json
import os
import re
import subprocess
import sys

_LIB = os.path.dirname(os.path.realpath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import claude_home as claude_home_mod  # noqa: E402
import hook_heartbeat  # noqa: E402
import profiles  # noqa: E402

# Bytes read from the END of a transcript, as tools/harness-check does: a session
# transcript grows without bound and the version sits near the end.
TRANSCRIPT_TAIL_BYTES = 262144

_SESSION_ID = re.compile(r"^[A-Za-z0-9-]+$")
_LEADING_VERSION = re.compile(r"^\s*([0-9]+\.[0-9]+\.[0-9]+)")

CONFIGURED = "configured"
INSTALLED = "installed"
PROVEN = "runtime-proven"
UNVERIFIED = "unverified"


class ReadinessError(Exception):
    pass


def session_version(home, session_id):
    """Most recent non-empty string `version` in this session's transcript, or None.
    Never raises."""
    try:
        if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
            return None
        pattern = os.path.join(glob.escape(claude_home_mod.claude_home(home)),
                               "projects", "*", session_id + ".jsonl")
        for path in sorted(glob.glob(pattern)):
            version = _tail_version(path)
            if version:
                return version
    except Exception:
        return None
    return None


def _tail_version(path):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            chunk = handle.read()
    except Exception:
        return None
    for line in reversed(chunk.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if isinstance(record, dict):
            version = record.get("version")
            if isinstance(version, str) and version:
                return version
    return None


def path_version():
    """Leading N.N.N of `claude --version`, or None. Display only. Never raises."""
    try:
        done = subprocess.run(["claude", "--version"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                              timeout=5, encoding="utf-8", errors="replace")
        match = _LEADING_VERSION.match(done.stdout or "")
        return match.group(1) if match else None
    except Exception:
        return None


def _read_settings(home):
    path = os.path.join(home, ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def report(repo, home, session_id):
    """{"tested", "session", "profiles": [{name, plugin, state, reason, needs}]}.
    Raises ReadinessError when it cannot be computed."""
    names, error, _defaulted = profiles.selected(repo)
    if error:
        raise ReadinessError(error)
    try:
        registry = profiles.load_registry(repo)
        tested = profiles.load_tested_version(repo)
    except profiles.RegistryError as exc:
        raise ReadinessError(str(exc))
    enabled, error = profiles.enabled_profiles(registry, _read_settings(home))
    if error:
        raise ReadinessError(error)
    session = session_version(home, session_id)
    rows = []
    for name in names:
        entry = registry[name]
        try:
            needs = profiles.required_features(repo, entry)
        except profiles.RegistryError as exc:
            raise ReadinessError(str(exc))
        row = {"name": name, "plugin": entry["plugin"], "reason": "", "needs": needs}
        if name not in enabled:
            row["state"] = CONFIGURED
            row["reason"] = "selected but not enabled in ~/.claude/settings.json"
        else:
            if name == "core":
                state, message = hook_heartbeat.verdict(repo, home, session_id)
            else:
                state, message = hook_heartbeat.profile_verdict(
                    repo, home, session_id, entry["dir"])
            row["reason"] = message
            if state != "proven":
                row["state"] = INSTALLED
            elif tested is not None and session == tested:
                row["state"] = PROVEN
            else:
                row["state"] = UNVERIFIED
                # Keep the verdict's own wording: core's says its hooks FIRED (a
                # canary), a profile's says only its SessionStart hooks ran. A
                # blanket "hooks proven" claimed more for profiles (H4 F8).
                row["reason"] = ("%s; but this session runs Claude Code %s and "
                                 "the tested version is %s"
                                 % (message, session or "unknown", tested or "unknown"))
        rows.append(row)
    return {"tested": tested, "session": session, "profiles": rows}


def exit_code(result):
    bad = (CONFIGURED, INSTALLED)
    return 1 if any(row["state"] in bad for row in result["profiles"]) else 0


def render(result, on_path):
    lines = ["Readiness: tested with Claude Code %s; this session runs %s; claude on PATH: %s"
             % (result["tested"] or "unknown", result["session"] or "unknown",
                on_path or "not found")]
    for row in result["profiles"]:
        line = "%s (%s): %s" % (row["name"], row["plugin"], row["state"])
        if row["reason"]:
            line += " — " + row["reason"]
        lines.append(line)
        if row["needs"]:
            lines.append("  needs: " + ", ".join(row["needs"]))
    return lines


def main(argv):
    if len(argv) != 2 or argv[0] != "report":
        sys.stderr.write("usage: readiness.py report <repo>\n")
        return 2
    try:
        result = report(argv[1], os.path.expanduser("~"),
                        os.environ.get("CLAUDE_CODE_SESSION_ID") or None)
    except ReadinessError as exc:
        sys.stderr.write("readiness cannot be computed: %s\n" % exc)
        return 2
    print("\n".join(render(result, path_version())))
    return exit_code(result)


if __name__ == "__main__":
    # Same as profiles.py: pin UTF-8 so a non-ASCII summary or path never fails on
    # a console set to a legacy code page.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
