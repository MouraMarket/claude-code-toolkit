#!/usr/bin/env python3
"""Print one hash naming the CONTENT of a checkout's working tree.

    python3 lib/checkout_fingerprint.py [<checkout>]

/onboard runs the test suite once, in the preview, and skips it on the install
and the final check only while this hash still matches the one taken around
that preview (row 84). So the hash has to change whenever anything the suites
could see changes, and only then:

- tracked files by content, not by `git status` letters (two different edits
  to one file both print ` M file`; Codex Code Review, row 84);
- untracked files too, whatever `status.showUntrackedFiles` says, because
  install.sh discovers suites with `find`, not git;
- ignored files never (the preview's own caches would break every match), and
  only the repository's ignore rules count: the user's global excludes file is
  switched off, so it cannot hide a new suite.

It stages everything into a throwaway index and prints `git write-tree`, so
the real index is never touched. The blobs it writes are ordinary loose
objects that `git gc` cleans up. Exit 0 with the hash, or 1 with a reason on
stderr; on 1, a caller must treat the checkout as untested.
"""

import os
import subprocess
import sys
import tempfile


def fingerprint(checkout):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    with tempfile.TemporaryDirectory() as scratch:
        env["GIT_INDEX_FILE"] = os.path.join(scratch, "index")
        base = ["git", "-C", checkout, "-c", "core.excludesFile=/dev/null"]
        # `:/` is the top of the working tree, whatever folder `checkout` names.
        for args in (["add", "--all", "--", ":/"], ["write-tree"]):
            done = subprocess.run(base + args, env=env, capture_output=True,
                                  encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL)
            if done.returncode != 0:
                raise RuntimeError(done.stderr.strip() or "git %s failed" % args[0])
        return done.stdout.strip()


def main(argv):
    if len(argv) > 2:
        print("usage: checkout_fingerprint.py [<checkout>]", file=sys.stderr)
        return 2
    try:
        print(fingerprint(argv[1] if len(argv) == 2 else "."))
    except (OSError, RuntimeError) as error:
        print("no fingerprint: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv))
