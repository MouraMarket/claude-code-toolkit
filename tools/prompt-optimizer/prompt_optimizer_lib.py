"""Build the PreToolUse reminder emitted before a subagent is dispatched.

A subagent starts cold with none of the parent conversation's context, so an
ambiguous or unstructured dispatch prompt fails silently: the subagent does
its best with what it was handed and there is no follow-up question. This
module builds the fixed reminder that keeps prompt optimization salient on
every spawn, wired as a PreToolUse hook on the Agent matcher.

Opt-in. `is_enabled` is True only when `prompt_optimizer.enabled` is the
literal JSON true. Absent config means OFF, because this is a per-dispatch
nudge rather than a guard, so the stricter default is silence.

The reminder names a skill, so it needs that skill installed. With the
reminder enabled and no skill to point at, `build` returns nothing: a
reminder to use a skill that is not there would only add noise. `status`
exists so the installer can say that state out loud, since the hook itself
stays silent by design.
"""

import json
import os
import sys

SKILL_FOLDER = "prompt-optimizer"

INERT_MESSAGE = (
    "inert: prompt_optimizer.enabled is true but no prompt-optimizer skill is "
    "installed, so the reminder stays silent. Install the skill, or set "
    "prompt_optimizer.enabled to false."
)


def _config():
    """Return the toolkit config module, or None if it cannot be imported.

    Imported lazily and defensively: a hook that crashes because a config
    helper moved is worse than a hook that silently does nothing.
    """
    try:
        import toolkit_config

        return toolkit_config
    except Exception:
        return None


def _plugin_paths():
    """Return the plugin_paths module, or None if it cannot be imported."""
    try:
        import plugin_paths

        return plugin_paths
    except Exception:
        return None


def is_enabled(config=None):
    """True ONLY when explicitly enabled. Absent config means OFF.

    The feature is a per-dispatch nudge, not a guard, so the stricter default
    is silence rather than an unsolicited reminder on every subagent spawn.
    Only a literal true counts: a string such as "true" or the number 1 must
    not enable it by accident.
    """
    if config is None:
        config = _config()
    if config is None:
        return False
    try:
        return config.get("prompt_optimizer", "enabled", default=False) is True
    except Exception:
        return False


def _is_file(path):
    try:
        return os.path.isfile(path)
    except Exception:
        return False


def find_skill(home):
    """The name to invoke the prompt-optimizer skill by, or None if absent.

    A skill in the user's own skills folder is invoked by its bare name. One
    shipped by an enabled plugin is invoked as `<plugin>:<skill>`. Detection
    keys on the skill's folder name rather than on a plugin name, so it keeps
    working whichever plugin ends up shipping the skill. Never raises: any
    unreadable or unexpected input means "not found".
    """
    try:
        if not isinstance(home, str) or not home:
            return None
        if _is_file(os.path.join(home, ".claude", "skills", SKILL_FOLDER, "SKILL.md")):
            return SKILL_FOLDER
        with open(os.path.join(home, ".claude", "settings.json"), encoding="utf-8") as handle:
            settings = json.load(handle)
        if not isinstance(settings, dict):
            return None
        enabled = settings.get("enabledPlugins")
        if not isinstance(enabled, dict):
            return None
        paths = _plugin_paths()
        if paths is None:
            return None
        for key in sorted(k for k, v in enabled.items() if isinstance(k, str) and v is True):
            root = paths.installed_root(home, key)
            if root and _is_file(os.path.join(root, "skills", SKILL_FOLDER, "SKILL.md")):
                return "%s:%s" % (key.partition("@")[0], SKILL_FOLDER)
        return None
    except Exception:
        return None


def reminder_text(skill):
    """The fixed reminder, naming the skill to invoke."""
    return (
        "prompt-optimizer: You are dispatching a subagent, which starts with none "
        "of this conversation's context. Before it runs, optimize the dispatch "
        "prompt with the /%s skill: give the subagent a clear role and objective, "
        "use positive framing over negatives, include the grounding materials and "
        "constraints it needs, specify an explicit output format for what it should "
        "hand back, and do not delegate work you could finish directly in a single "
        "response. If the prompt is already clear, self-contained, and well-scoped, "
        "proceed as-is."
    ) % skill


def payload(skill):
    """The PreToolUse additionalContext payload, as a dict."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": reminder_text(skill),
        }
    }


def build(config=None, home=None):
    """Full pipeline: config and skill to the JSON text to print, or ''.

    Config is read first and the home is touched only when enabled, so a
    disabled reminder reads nothing beyond the config.
    """
    if not is_enabled(config):
        return ""
    skill = find_skill(home if home is not None else os.path.expanduser("~"))
    if skill is None:
        return ""
    return json.dumps(payload(skill))


def status(config=None, home=None):
    """(state, skill): "off", "on" with the skill, or "inert" with None."""
    if not is_enabled(config):
        return ("off", None)
    skill = find_skill(home if home is not None else os.path.expanduser("~"))
    if skill is None:
        return ("inert", None)
    return ("on", skill)


def main(argv):
    if list(argv) != ["status"]:
        sys.stderr.write("usage: prompt_optimizer_lib.py status\n")
        return 2
    here = os.path.dirname(os.path.realpath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(here)), "lib"))
    try:
        state, skill = status()
    except Exception:
        return 0
    if state == "on":
        print("on: reminds before each subagent dispatch to use /%s" % skill)
    elif state == "inert":
        print(INERT_MESSAGE)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
