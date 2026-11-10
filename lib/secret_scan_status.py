#!/usr/bin/env python3
"""Is commit secret scanning active on this machine? Prints one word.

    python3 lib/secret_scan_status.py <checkout>

`active`  global core.hooksPath is the toolkit's hooks dir and its pre-commit
          resolves to <checkout>/tools/secret-scan/git-pre-commit.
`unset`   no global core.hooksPath: commits are not scanned.
`foreign` core.hooksPath is someone else's; we cannot see whether their
          hook calls the scanner, so we never claim it does.
`stale`   it is ours, but the pre-commit is missing, broken or not this
          checkout's.

Read-only, and fast enough for `/onboard verify`. install.sh's "Secret
scanning" section prints from the same word, so the installer and the
onboard summary cannot disagree (row 74: a summary said secrets were blocked
while `--check` said NOT active). Anything but `active` means OFF.
"""

import os
import subprocess
import sys


def toolkit_hooks_dir():
    return os.path.join(os.path.expanduser("~"), ".config", "claude-code-toolkit", "git-hooks")


def state(checkout):
    try:
        hooks = subprocess.run(
            ["git", "config", "--global", "--get", "core.hooksPath"],
            capture_output=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL).stdout.strip()
    except OSError:
        hooks = ""
    if not hooks:
        return "unset"
    hooks = os.path.expanduser(hooks)
    ours = toolkit_hooks_dir()
    if os.path.realpath(hooks) != os.path.realpath(ours):
        return "foreign"
    hook = os.path.join(hooks, "pre-commit")
    expected = os.path.join(checkout, "tools", "secret-scan", "git-pre-commit")
    if (os.path.realpath(hook) == os.path.realpath(expected)
            and os.path.isfile(hook) and os.access(hook, os.X_OK)):
        return "active"
    return "stale"


def main(argv):
    if len(argv) != 2:
        print("usage: secret_scan_status.py <checkout>", file=sys.stderr)
        return 2
    print(state(argv[1]))
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv))
