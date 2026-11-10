#!/usr/bin/env python3
"""Check the capability registry (docs/capabilities.json), and with
--require-evidence prove each capability's tests actually ran.

The silent failure this guards: a capability reads as tested because its suite
file exists, or because the suite exited 0 having run zero tests or skipped
everything. Nothing else complains. See lib/capability_evidence.py.

Usage:
    check-capabilities.py [--all] [--only ID ...] [--require-evidence]
                          [--live-evidence FILE] [--out REPORT.json]
                          [--repo PATH] [--jobs N]

Without --require-evidence: the structural checks only (never runs a suite),
including a suite no capability owns; exit 0 clean, 1 on any structural problem. With it: structural checks plus a
run of every selected capability's suites under Python 3.9 and 3.13; exit 0
only when every selected capability is evidenced and nothing global failed,
1 otherwise, 2 when the environment is not fit for a release run (no Node, a
missing interpreter, a dirty tree, a bad allowlist or live file) or on a usage
error.

A release pass needs --live-evidence: without it every skill is `awaiting-live`.
The file records a live run of each skill in Claude Code:

    {"schema": 2, "claude_version": "<claude --version>",
     "records": [{"id": "skill:<name>",
                  "identity": {"closure_sha256": "<--identities digest of the tree run>"},
                  "cases": [
         {"kind": "positive" | "out-of-scope" | "reference",
          "observed": "pass" | "fail",
          "outcome": "completed" | "gate-stop" | "env-blocked",  (positive: required)
          "transcript": "<path relative to FILE>", "sha256": "<hex of the transcript>"}]}]}

A skill's record counts when it has at least one case of each kind, every case
observed `pass`, no case `env-blocked` (a permission or environment block is
never a pass), every transcript exists with a matching sha256, and its identity
equals the candidate's: a digest of every file the skill runs or reads (its
folder minus tests/, its plugin manifest, the paths and `claude-*` tools its
text names, its non-skill `requires`, and their imports). A `gate-stop` positive
case passes with a note: it certifies stopping at the gate, nothing past it.
Schema 1 receipts carry no identity and are refused.

--identities prints each skill's identity as JSON and exits; it runs no suite
and needs no git, so it works on an exported tree.
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys

SCRIPT_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(SCRIPT_REPO, "lib"))
import capability_evidence as ev  # noqa: E402

CACHE = os.path.join(os.path.expanduser("~"), ".cache")
VERSIONS = ("3.9", "3.13")
SKIPS_REL = "docs/capability-skips.json"
STRUCTURAL_CHECKS = ("check_schema", "check_ids", "check_paths", "check_delivery",
                     "check_config", "check_requires", "check_coverage",
                     "check_unowned_suites", "check_tool_entry_points", "check_private")


def load_capabilities(repo):
    """lib/capabilities.py of `repo`, so the checks run against that repo."""
    lib = os.path.join(repo, "lib")
    path = os.path.join(lib, "capabilities.py")
    if not os.path.isfile(path):
        raise ValueError("%s does not exist" % path)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    spec = importlib.util.spec_from_file_location("capabilities_of_%d" % abs(hash(repo)), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def structural_problems(repo):
    try:
        caps = load_capabilities(repo)
        registry = caps.load_registry()
    except (ValueError, OSError, ImportError) as err:
        return ["cannot load the registry: %s" % err], None
    problems = []

    def run(name, *args):
        try:
            problems.extend(getattr(caps, name)(registry, *args))
        except Exception as err:  # a malformed registry can break later checks
            problems.append("%s crashed: %s: %s" % (name, type(err).__name__, err))

    for name in STRUCTURAL_CHECKS:
        run(name)
    try:
        commands = caps.manifest_commands()
        relpaths = caps.hook_relpaths()
    except Exception as err:
        problems.append("cannot read install.sh --manifest or hooks: %s: %s"
                        % (type(err).__name__, err))
    else:
        run("check_commands", commands)
        run("check_hooks", relpaths)
    # The allowlist ships too, so its reasons are held to the registry's rule.
    skips = os.path.join(repo, SKIPS_REL)
    if os.path.isfile(skips):
        try:
            with open(skips, encoding="utf-8") as handle:
                problems.extend("%s: %s" % (SKIPS_REL, p)
                                for p in caps.check_private(json.load(handle)))
        except (OSError, ValueError) as err:
            problems.append("%s: cannot read: %s" % (SKIPS_REL, err))
    return problems, registry


def refuse(reason):
    print("check-capabilities: refused: %s" % reason)
    return 2


def first_reason(info):
    line = ""
    if info["reasons"]:
        line = ": " + info["reasons"][0]
        if len(info["reasons"]) > 1:
            line += " (+%d more)" % (len(info["reasons"]) - 1)
    return line


def log_tail(path, lines=60):
    """The end of a suite's output, so a red suite in a report names its cause
    after the run's scratch directory is gone."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()[-lines:]
    except OSError as err:
        return ["(log unreadable: %s)" % err]


def suite_summary(result, tails):
    tests = result["record"] or []
    summary = {"suite": result["suite"], "version": result["version"], "rc": result["rc"],
               "timed_out": result["timed_out"], "tests": len(tests),
               "passed": sum(1 for t in tests if t.get("outcome") == "pass"),
               "skipped": sum(1 for t in tests if t.get("outcome") == "skip")}
    key = (result["suite"], result["version"])
    if key in tails:
        summary["log_tail"] = tails[key]
    return summary


def evidence_run(args, repo, registry, structural):
    """Release mode after the structural pass; returns the exit code."""
    node = ev.node_version()
    if node is None:
        return refuse("node is not available (node --version failed)")
    pythons = {}
    for version in VERSIONS:
        path = ev.resolve_interpreter(version)
        if path is None:
            return refuse("python %s does not resolve (uv python find --system %s, or "
                          "python%s on PATH)" % (version, version, version))
        pythons[version] = {"path": path, "version": ev.interpreter_version(path)}
    try:
        head, tree, porcelain = ev.git_state(repo)
    except (RuntimeError, OSError) as err:
        return refuse("%s is not a usable git repository: %s" % (repo, err))
    if porcelain.strip():
        return refuse("the working tree is dirty; commit or clean it first")
    live = {"by_id": {}, "errors": []}
    if args.live_evidence:
        try:
            live = ev.load_live(args.live_evidence,
                                [c["id"] for c in registry["capabilities"]])
        except (ValueError, OSError) as err:
            return refuse("live evidence file: %s" % err)
    skips_path = os.path.join(repo, SKIPS_REL)
    if not os.path.isfile(skips_path):
        return refuse("%s is missing" % SKIPS_REL)
    try:
        allowlist = ev.load_allowlist(skips_path)
    except (ValueError, OSError) as err:
        return refuse("%s: %s" % (SKIPS_REL, err))

    caps = {c["id"]: c for c in registry["capabilities"]}
    selected = list(caps) if not args.only else list(args.only)
    suites = set()
    for ident in selected:
        for rel in caps.get(ident, {}).get("tests", []):
            suites.update(ev.suites_for(repo, rel))
    if not args.only:
        for entry in allowlist:
            suites.update(ev.suites_for(repo, entry["suite"]))
    jobs = args.jobs or min(8, os.cpu_count() or 1)

    root = os.path.join(os.path.realpath(CACHE), "check-capabilities-%d" % os.getpid())
    os.makedirs(root, exist_ok=True)
    try:
        results = ev.run_all(repo, sorted(suites),
                             {v: p["path"] for v, p in pythons.items()}, root, jobs, 1200)
        tails = {(r["suite"], r["version"]): log_tail(r["log"]) for r in results
                 if r["rc"] != 0 or r["timed_out"] or r["record"] is None}
    finally:
        shutil.rmtree(root, ignore_errors=True)

    verdict = ev.judge(registry, results, allowlist, live, selected, not args.only, repo)
    try:
        after = ev.git_state(repo)
    except (RuntimeError, OSError) as err:
        after = None
        verdict["global"].append("the run changed the repository: cannot read git state: %s"
                                 % err)
    if after is not None and (after[0] != head or after[2] != porcelain):
        verdict["global"].append("the run changed the repository: HEAD %s -> %s, status %r"
                                 % (head[:12], after[0][:12], after[2].strip()[:200]))

    counts = {"evidenced": 0, "failed": 0, "awaiting-live": 0}
    for ident in selected:
        info = verdict["capabilities"].get(ident)
        if info is None:
            continue
        counts[info["state"]] += 1
        print("%s %s%s" % (info["state"], ident, first_reason(info)))
        for note in info.get("notes", []):
            print("  note: %s" % note)
    for problem in verdict["global"]:
        print("FAIL global: %s" % problem)
    print("%d capabilities: %d evidenced, %d failed, %d awaiting-live"
          % (len(selected), counts["evidenced"], counts["failed"], counts["awaiting-live"]))

    if args.out:
        report = {"schema": 1, "head": head, "tree": tree, "pythons": pythons, "node": node,
                  "live_evidence": args.live_evidence or None,
                  "capabilities": verdict["capabilities"], "global": verdict["global"],
                  "stale": verdict["stale"], "unowned": verdict["unowned"],
                  "structural": structural,
                  "suites": [suite_summary(r, tails) for r in results]}
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
    ok = counts["evidenced"] == len(selected) and not verdict["global"] and not structural
    return 0 if ok else 1


def print_identities(repo):
    try:
        registry = load_capabilities(repo).load_registry()
    except (ValueError, OSError, ImportError) as err:
        return refuse("cannot load the registry: %s" % err)
    caps = {c["id"]: c for c in registry["capabilities"]}
    out = {}
    for ident in sorted(i for i, c in caps.items() if c["kind"] == "skill"):
        try:
            out[ident] = ev.skill_identity(repo, caps[ident], caps)
        except (ValueError, OSError) as err:
            return refuse("%s: cannot compute implementation identity: %s" % (ident, err))
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="check-capabilities", description=__doc__.split("\n")[0])
    parser.add_argument("--all", action="store_true", help="every capability (the default)")
    parser.add_argument("--only", action="append", default=[], metavar="ID",
                        help="one capability id; repeatable")
    parser.add_argument("--require-evidence", action="store_true",
                        help="also run the suites and judge what happened")
    parser.add_argument("--live-evidence", metavar="FILE")
    parser.add_argument("--out", metavar="REPORT.json")
    parser.add_argument("--repo", default=SCRIPT_REPO)
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--identities", action="store_true",
                        help="print each skill's implementation identity as JSON and exit")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 2
    if args.all and args.only:
        print("check-capabilities: --all and --only cannot be combined", file=sys.stderr)
        return 2
    repo = os.path.realpath(args.repo)
    if args.identities:
        if args.require_evidence:
            print("check-capabilities: --identities and --require-evidence cannot be combined",
                  file=sys.stderr)
            return 2
        return print_identities(repo)

    structural, registry = structural_problems(repo)
    if not args.require_evidence:
        for problem in structural:
            print("FAIL structural: %s" % problem)
        return 1 if structural else 0
    if registry is None:
        for problem in structural:
            print("FAIL structural: %s" % problem)
        return refuse("the registry cannot be loaded")
    for problem in structural:
        print("FAIL structural: %s" % problem)
    return evidence_run(args, repo, registry, structural)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
