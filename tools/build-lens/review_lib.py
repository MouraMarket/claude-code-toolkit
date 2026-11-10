"""The Agent runtime for /build's review gates: the prompt and the report check.

`blinded-review.js` runs a gate through the `Workflow` tool: it builds the
reviewer's prompt, the runtime forces the report schema, and the script throws
on a report that describes a review that never happened. Where `Workflow` is
absent, a gate dispatches one Agent instead, and this module is what keeps that
the same review:

- `build_prompt` returns the exact string `blinded-review.js` passes to
  `agent()`, with the same refusals. `tests/test_review_gate.py` compares the
  two by running the script under Node, so a change to either side reddens a
  test rather than quietly forking the two runtimes' prompts.
- `validate_against_schema` stands in for the runtime's schema forcing, after
  the fact: it cannot prevent a malformed report, only refuse one.
- `semantic_violation` is the rule the JavaScript copy in `blinded-review.js`
  also carries. Both are held to `tests/semantic_cases.json`; change them
  together.

Stdlib only.
"""
import json
import os
import re
from pathlib import Path

GATES = ("design", "plan", "code", "merge")

# `None` is a plain agent: design and plan use no agent type because
# code-reviewer's description forbids it as the sole reviewer of a plan and
# its rubric is diff-ordered (the same choice blinded-review.js makes).
AGENT_TYPES = {
    "design": None,
    "plan": None,
    "code": "toolkit-build:code-reviewer",
    "merge": "toolkit-build:code-reviewer",
}

REPORT_SCHEMA_PATH = Path(os.path.realpath(__file__)).parent / "merge-review-report.schema.json"

_DRIVE_PATH = re.compile(r"^[A-Za-z]:/")


def _js_stringify(value):
    """`JSON.stringify` as the script's error messages print a value."""
    if value is None:
        return "undefined"
    return json.dumps(value, ensure_ascii=False)


def _is_absolute_path(path):
    # Same test as the script: a forward-slash drive-letter path is absolute
    # too; backslash spellings stay refused (the reviewer's tools take
    # forward slashes everywhere).
    return (isinstance(path, str) and path.strip() == path and len(path) > 1
            and (path.startswith("/") or bool(_DRIVE_PATH.match(path))))


def _checklist(gate, lens_dir):
    return {
        "design": 'the table under "### The lens table" in %s/design-review.md' % lens_dir,
        "plan": 'the table under "### Plan Review lenses" in %s/plan-review.md' % lens_dir,
        "code": 'every lens table under "### Code Review lenses" in %s/code-review.md' % lens_dir,
        "merge": None,
    }[gate]


_ARTIFACT = {"design": "design", "plan": "plan", "code": "change", "merge": "change"}

UNREADABLE = (
    "If any file named here is missing, unreadable or empty, do not review: return "
    "recommendation REQUEST_CHANGES, executed.ran false, executed.why_not naming the file, "
    "and exactly one BLOCKING finding naming it. Never return an empty findings list "
    "for a review you could not perform.")


def build_prompt(gate, brief_path, lens_dir, plan_path=None):
    """The prompt `blinded-review.js` hands its reviewer for these arguments.

    Raises ValueError, with the script's own message, for every argument set
    the script refuses, checked in the script's order.
    """
    if not isinstance(gate, str) or gate not in GATES:
        raise ValueError("gate must be one of %s; got %s"
                         % (", ".join(GATES), _js_stringify(gate)))
    if not _is_absolute_path(lens_dir):
        raise ValueError("lensDir must be an absolute path to the directory holding "
                         "the gate references; got %s" % _js_stringify(lens_dir))
    if not _is_absolute_path(brief_path):
        raise ValueError("briefPath must be an absolute path to the brief file; got %s"
                         % _js_stringify(brief_path))
    if gate == "code" and not _is_absolute_path(plan_path):
        raise ValueError('gate "code" requires planPath, the absolute path to the plan; '
                         "got %s" % _js_stringify(plan_path))
    if gate != "code" and plan_path is not None:
        raise ValueError('planPath is accepted only at gate "code" -- every other gate '
                         "is blinded to the plan")

    framing = {
        "design": ("Review the design described in %s. That file is your ONLY framing -- it holds "
                   "the design and the requirements it answers. Do not read any other spec, plan, ledger or "
                   "handover about it, and do not go looking for one." % brief_path),
        "plan": ("Review the plan described in %s. That file is your ONLY framing -- it holds "
                 "the plan and the requirements it must satisfy. Do not read any other spec, design "
                 "document, ledger or handover about it, and do not go looking for one." % brief_path),
        "code": ("Review the change described in %s. The plan it was built from is %s; "
                 "it is the contract the change is judged against. Those two files are your ONLY framing -- "
                 "do not read any spec, design document, ledger or handover, and do not go looking for one."
                 % (brief_path, plan_path)),
        "merge": ("Review the change described in %s. That file is your ONLY framing -- "
                  "do not read the plan, the spec, or any design document, and do not go looking for one."
                  % brief_path),
    }[gate]
    artifact = _ARTIFACT[gate]
    if gate in ("design", "plan"):
        execute = ("Where the %s names a command, a file or a behaviour of existing code, "
                   "run or read it to check the claim; report exactly what you ran." % artifact)
    else:
        execute = ("Run the repo's suite and attempt to falsify the changed behaviour; "
                   "report exactly what you ran.")
    checklist = _checklist(gate, lens_dir)
    checklist_text = ("Your checklist is %s Apply EVERY row. Assume the %s is broken and find where."
                      % (checklist, artifact)) if checklist else ""
    return " ".join(part for part in (framing, checklist_text, execute, UNREADABLE) if part)


def report_clause(report_path, schema_path=REPORT_SCHEMA_PATH):
    """What the Agent runtime adds after the prompt: where the report goes.

    The Workflow runtime returns the report itself; an Agent's reply is prose,
    so the reviewer writes the report to a file and the dispatcher checks that
    file. The reviewer writes it, not the dispatcher: a report transcribed by
    the party under review is not the reviewer's.
    """
    return ("Write your report as a single JSON object matching the schema in %s "
            "(the keys recommendation, executed and findings, nothing else) to %s, using your "
            "shell, and write nothing else to that file. Then reply with that path." % (
                schema_path, report_path))


_SCHEMA_TYPE_TO_PYTHON = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
}


def _validate_node(schema, value, path, errors):
    """Append a human-readable violation to `errors` for every mismatch
    between `schema` and `value`, recursing through `properties` and
    `items`. Never raises -- a malformed schema or value produces a
    violation string, not an exception."""
    if not isinstance(schema, dict):
        return

    expected_type = schema.get("type")
    if expected_type is not None:
        python_type = _SCHEMA_TYPE_TO_PYTHON.get(expected_type)
        if python_type is not None:
            # `bool` is a subclass of `int` in Python, so a `number`/`integer`
            # schema must not silently accept a boolean disguised as one.
            if expected_type in ("number", "integer") and isinstance(value, bool):
                errors.append("{}: expected {}, got boolean".format(path, expected_type))
                return
            if not isinstance(value, python_type):
                errors.append("{}: expected {}, got {}".format(
                    path, expected_type, type(value).__name__))
                return

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append("{}: {!r} is not one of {}".format(path, value, enum))

    if isinstance(value, dict):
        properties = schema.get("properties")
        declared = properties if isinstance(properties, dict) else {}

        if isinstance(properties, dict):
            for key in schema.get("required") or []:
                if key not in value:
                    errors.append("{}: missing required key {!r}".format(path, key))
            for key, subschema in declared.items():
                if key in value:
                    _validate_node(subschema, value[key], "{}.{}".format(path, key), errors)

        # The report schema declares `additionalProperties: false` at every
        # object level, so it is checked at EVERY recursion, not just the
        # root: a check applied only at the root leaves `executed` and each
        # `findings[]` element unchecked. `True` or absent both permit
        # undeclared keys, so the comparison is an identity check against
        # `False` -- a truthy-but-not-`False` value (a string, a list) can
        # never be misread as "forbid". `declared` falls back to `{}` when
        # `properties` is absent, so a `{}`-shaped sub-schema with
        # `additionalProperties: false` rejects any key rather than crashing.
        # This function must never raise, whatever `additionalProperties`
        # holds.
        try:
            if schema.get("additionalProperties") is False:
                for key in value:
                    if key not in declared:
                        errors.append("{}: additional property {!r} is not allowed".format(
                            path, key))
        except BaseException:
            pass

    if isinstance(value, list) and schema.get("items") is not None:
        for index, item in enumerate(value):
            _validate_node(schema["items"], item, "{}[{}]".format(path, index), errors)


def validate_against_schema(schema, value):
    """A minimal, stdlib-only structural validator: required keys present,
    enums respected, types right, `additionalProperties: false` enforced at
    every object level, recursing through `properties` and `items`. Not a
    general JSON Schema implementation: it covers exactly the shapes the
    review report schema uses, and needs no third-party module.

    Returns a list of violation strings; an empty list means `value`
    conforms to `schema`. Callers check `[]` versus non-empty, never an
    exception, to tell conforming from not -- this function never raises.
    """
    errors = []
    _validate_node(schema, value, "<root>", errors)
    return errors


def semantic_violation(report):
    """None if `report` describes a review that plausibly happened; else a
    human-readable reason it does not.

    A structurally valid report can still describe a review that never
    happened -- `{"executed": {"ran": false, "commands": [], "why_not": ""},
    "findings": []}` satisfies every type and enum in the schema. This is
    the check that catches that:

      - `executed.ran` is false AND `findings` is empty -- nothing ran and
        nothing was found, indistinguishable from a broken gate.
      - `executed.why_not` is empty/whitespace when `executed.ran` is false.
      - `executed.ran` is true AND `executed.commands` is empty.
      - `executed.ran` is true AND any `executed.commands` entry is blank
        or not a string -- a command nobody can re-run.
      - `recommendation` is APPROVE AND any finding is BLOCKING -- a report
        cannot approve what it says blocks the merge.

    `ran: false` with a real `why_not` and real findings is legitimate -- a
    repository with no suite has nothing to run -- and passes here. The rule
    targets silence, not absence of execution.

    `blinded-review.js` carries a JavaScript copy of this same rule for the
    Workflow runtime; change the two together. Both are tested against
    `tests/semantic_cases.json`.
    """
    executed = report.get("executed") if isinstance(report, dict) else None
    if not isinstance(executed, dict):
        return "executed is missing or not an object"
    ran = executed.get("ran")
    why_not = executed.get("why_not")
    commands = executed.get("commands")
    findings = report.get("findings")

    if ran is False:
        if not findings:
            return ("executed.ran is false and findings is empty -- "
                    "indistinguishable from a broken gate")
        if not isinstance(why_not, str) or not why_not.strip():
            return ("executed.ran is false and executed.why_not is empty "
                    "or whitespace")
    if ran is True:
        if not isinstance(commands, list) or not commands:
            return "executed.ran is true and executed.commands is empty"
        if any(not isinstance(c, str) or not c.strip() for c in commands):
            return ("executed.ran is true and executed.commands has a blank "
                    "entry -- a command nobody can re-run")
    if report.get("recommendation") == "APPROVE" and isinstance(findings, list):
        blocking = sum(1 for f in findings
                       if isinstance(f, dict) and f.get("severity") == "BLOCKING")
        if blocking:
            return ("recommendation is APPROVE but %d finding(s) are BLOCKING "
                    "-- a report cannot approve what it says blocks the merge"
                    % blocking)
    return None


def summary_line(report):
    """The line `check` prints on acceptance; the build ledger copies it."""
    counts = {"BLOCKING": 0, "IMPORTANT": 0}
    for finding in report.get("findings") or []:
        if isinstance(finding, dict) and finding.get("severity") in counts:
            counts[finding["severity"]] += 1
    return "accepted: %s, executed.ran=%s, BLOCKING=%d, IMPORTANT=%d" % (
        report.get("recommendation"),
        "true" if report["executed"].get("ran") is True else "false",
        counts["BLOCKING"], counts["IMPORTANT"])
