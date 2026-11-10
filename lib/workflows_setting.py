#!/usr/bin/env python3
"""The pure planner behind the `enableWorkflows` setting.

`install.sh`, `toolkit_update` and `uninstall` each need the same answer --
"given what the user wants, what the install record says the toolkit owns, and
what is in settings.json, what should happen?" -- so the rule lives here once.

No I/O and no function raises. Every comparison of a settings value is
type-strict (`is True`): JSON `true` and `1` compare equal in Python, and a
user's `1` must never be mistaken for the value the toolkit wrote.

Standard library only.
"""

# Sentinel for "the enableWorkflows key is absent from settings.json". A
# present JSON `null` is NOT this. Callers read the raw setting with
# `settings.get(workflows_setting.KEY, workflows_setting.ABSENT)`.
ABSENT = object()

KEY = "enableWorkflows"
RECORD_KEY = "enable_workflows_added"
CONFIG_PATH = ("build", "enable_workflows")


def wanted(config_value, selected_profiles):
    """True only for the JSON boolean `true` in the config AND the builder
    profile selected. A string "true", `1` or `null` is not."""
    return config_value is True and "builder" in (selected_profiles or ())


def owned(record):
    """True when the install record says the toolkit wrote the key."""
    return isinstance(record, dict) and record.get(RECORD_KEY) is True


def plan(current, wanted, owned):
    """One of "set", "foreign", "ok", "edited", "remove".

    `current` is the settings value or `ABSENT`; `wanted` and `owned` are bools.
    """
    if wanted and not owned:
        return "set" if current is ABSENT else "foreign"
    if owned:
        if current is True:
            return "ok" if wanted else "remove"
        return "edited"
    return "ok"


def owned_and_true(record, settings):
    """The predicate uninstall reuses: the toolkit set it and it is still
    exactly `true`."""
    return (owned(record) and isinstance(settings, dict)
            and settings.get(KEY, ABSENT) is True)
