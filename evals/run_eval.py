#!/usr/bin/env python3
"""Run a skill's eval cases and report pass rate and token cost.

    python3 evals/run_eval.py humanizer --dry-run
    python3 evals/run_eval.py humanizer --live --model MODEL
    python3 evals/run_eval.py humanizer --live --model MODEL --trials 3
    python3 evals/run_eval.py humanizer --live --model MODEL --price IN OUT
    python3 evals/run_eval.py humanizer --dry-run --skill-file /path/to/old-SKILL.md

Exactly one of `--dry-run` or `--live` is required. A dry run loads and
validates the skill and its cases and makes no API call and no network call.
`--live` spends your money, so it is always an explicit choice: it reads the
key only from the ANTHROPIC_API_KEY environment variable and needs `--model`.

Cost: no price table ships, because prices change. Token counts are always
reported. Pass `--price IN OUT` (USD per million input and output tokens, from
your own pricing page) and the dollar figures are computed at those rates and
labelled as such; without it every cost prints as unknown.

Exit codes: 0 all cases passed every trial, 1 something failed, 2 could not run
(bad arguments, missing key, unreadable skill, or an invalid or empty case file).

## What this measures, and what it does not

It measures the skill's **generation discipline**: the rules its own text
already states in countable form. It sends no tools, so it does not measure any
step that needs a lookup; the model answers from what it knows. That is a
deliberate trade: tools would make each run slower, costlier and
non-deterministic in a way unrelated to the skill's instructions, and an eval
that is expensive to run stops being run.

**Multiple trials matter.** Output is non-deterministic; a single run of a
configuration is a sample, not a measurement. `--trials 3` is a reasonable floor
for noticing a real regression.

## Coverage, and the two flags that make it measurable

The checks grade *discipline*. By construction they cannot see whether a change
quietly lost legitimate content: a tightened rule that suppresses good output
alongside bad scores a cleaner pass rate than the version that was right.
Answering that needs the output itself, from two versions of the same skill:

- `--json` emits `term_list` per result, the actual extracted terms. The `terms`
  field stays a count so the printed table and saved artifacts read the same way.
- `--skill-file` grades an arbitrary SKILL.md instead of the one in the tree, so
  an older revision can be measured *by today's grader*. Checking the old
  commit out instead would revert the grader too, and the two sides would no
  longer be comparable.
"""

import argparse
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath: may run via symlink
sys.path.insert(0, HERE)

import eval_lib  # noqa: E402

REPO_ROOT = os.path.dirname(HERE)


def find_skill(name):
    """The SKILL.md for a skill, in whichever place in this repo holds it."""
    patterns = (
        os.path.join(REPO_ROOT, "skills", "*", name, "SKILL.md"),
        os.path.join(REPO_ROOT, "plugins", name, "skills", name, "SKILL.md"),
    )
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    raise eval_lib.EvalError("no SKILL.md for %r in this repo; pass --skill-file" % name)


def load_cases(name, path=None):
    """The cases for a skill, validated. Never returns an empty or vacuous list."""
    path = path or os.path.join(HERE, "cases", "%s.json" % name)
    if not os.path.exists(path):
        raise eval_lib.EvalError("no case file at %s" % path)
    try:
        with open(path, encoding="utf-8") as handle:
            cases = json.load(handle)
    except ValueError as error:
        raise eval_lib.EvalError("%s is not valid JSON: %s" % (path, error))
    if not isinstance(cases, list) or not cases:
        raise eval_lib.EvalError("%s must hold a non-empty list of cases" % path)
    problems = []
    for case in cases:
        problems += eval_lib.validate_case(case)
    if problems:
        raise eval_lib.EvalError("invalid cases in %s: %s" % (path, "; ".join(problems)))
    return cases


def strip_frontmatter(text):
    """The skill body, without the YAML frontmatter.

    Frontmatter is routing metadata for the loader — a description written to
    make Claude *pick* the skill. Feeding it back as a system prompt would grade
    the trigger text, which is not what this measures.
    """
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def run_case(case, system, model, api_key, rates, dry_run):
    if dry_run:
        return {"case": case["name"], "passed": None, "failures": [],
                "terms": 0, "term_list": [], "cost": None, "dry_run": True}
    try:
        text, usage = eval_lib.call_model(system, case["prompt"], model, api_key)
    except eval_lib.EvalError as error:
        return {"case": case["name"], "passed": False,
                "failures": ["call failed: %s" % error],
                "terms": 0, "term_list": [], "cost": None}

    passed, failures, terms = eval_lib.grade(text, case)
    return {
        "case": case["name"],
        "passed": passed,
        "failures": failures,
        # Count and list both: `terms` is what the printed table reports,
        # `term_list` is what a coverage diff needs. Discarding the terms would
        # cost a re-run of every configuration to answer a question the first
        # run had the data for.
        "terms": len(terms),
        "term_list": terms,
        "cost": eval_lib.usage_cost(usage, rates),
        "usage": usage,
    }


def token_totals(results):
    """(input, output) tokens summed over every result that made a call."""
    total_in = total_out = 0
    for result in results:
        usage = result.get("usage") or {}
        total_in += (usage.get("input_tokens", 0)
                     + usage.get("cache_read_input_tokens", 0)
                     + usage.get("cache_creation_input_tokens", 0))
        total_out += usage.get("output_tokens", 0)
    return total_in, total_out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skill")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="validate the skill and cases; no API or network call")
    mode.add_argument("--live", action="store_true",
                      help="call the API (needs --model and ANTHROPIC_API_KEY)")
    parser.add_argument("--model", help="model id to call; required with --live")
    parser.add_argument("--price", nargs=2, type=float, metavar=("IN", "OUT"),
                        help="your USD per million input and output tokens; "
                             "without it costs print as unknown")
    parser.add_argument("--trials", type=int, default=1,
                        help="runs per case (default 1; use 3+ before acting on a diff)")
    parser.add_argument("--skill-file",
                        help="grade this SKILL.md instead of the one in the tree "
                             "(for measuring an older revision with today's grader)")
    parser.add_argument("--cases", help="case file to use instead of evals/cases/<skill>.json")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    # Everything that can refuse does so before the first call.
    api_key = None
    if args.live:
        if not args.model:
            print("cannot run: --live needs --model", file=sys.stderr)
            return 2
        try:
            api_key = eval_lib.resolve_api_key()
        except eval_lib.EvalError as error:
            print("cannot run: %s" % error, file=sys.stderr)
            return 2
    if args.trials < 1:
        print("cannot run: --trials must be at least 1", file=sys.stderr)
        return 2
    rates = tuple(args.price) if args.price else None

    try:
        skill_path = args.skill_file or find_skill(args.skill)
        if not os.path.exists(skill_path):
            raise eval_lib.EvalError("no file at %s" % skill_path)
        cases = load_cases(args.skill, args.cases)
    except eval_lib.EvalError as error:
        print("cannot run: %s" % error, file=sys.stderr)
        return 2

    with open(skill_path, encoding="utf-8") as handle:
        system = strip_frontmatter(handle.read())

    # stderr, so it survives `--json > file` and a result artifact can still be
    # traced back to the skill text that produced it.
    print("grading %s" % skill_path, file=sys.stderr)

    results = []
    for trial in range(args.trials):
        for case in cases:
            result = run_case(case, system, args.model, api_key, rates, args.dry_run)
            result["trial"] = trial + 1
            results.append(result)
            if not args.json and not args.dry_run:
                mark = "PASS" if result["passed"] else "FAIL"
                cost = result["cost"]
                print("  %-4s %-28s %3d terms  %s"
                      % (mark, result["case"], result["terms"],
                         "$%.4f (at your --price rates)" % cost
                         if cost is not None else "cost unknown"))
                for failure in result["failures"]:
                    print("         - %s" % failure)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if args.dry_run or all(r["passed"] for r in results) else 1

    if args.dry_run:
        shown = os.path.relpath(skill_path, REPO_ROOT)
        if shown.startswith(".."):  # --skill-file outside the repo
            shown = skill_path
        print("dry run: %s -> %d cases, %d checks total"
              % (shown, len(cases), sum(len(c.get("checks", {})) for c in cases)))
        return 0

    passed = sum(1 for r in results if r["passed"])
    costs = [r["cost"] for r in results if r["cost"] is not None]
    total_in, total_out = token_totals(results)
    print()
    print("pass rate      %d/%d" % (passed, len(results)))
    print("tokens         %d in, %d out" % (total_in, total_out))
    if costs:
        print("cost per case  $%.4f mean  (at your --price rates)" % statistics.mean(costs))
        print("total          $%.4f  (%s, %d trial%s, at your --price rates)"
              % (sum(costs), args.model, args.trials, "" if args.trials == 1 else "s"))
    else:
        print("cost           unknown (pass --price IN OUT to compute one)")
    if args.trials == 1 and passed < len(results):
        print()
        print("Single trial: output is non-deterministic. Re-run with --trials 3")
        print("before treating a failure as a regression.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
