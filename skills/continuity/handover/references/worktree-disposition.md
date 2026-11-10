# Worktree disposition

Read from the handover skill's §0. When you record whether this thread's worktree is paused or finished.

You are the only party that knows whether this thread stopping means *finished*
or *paused*. Nothing on the machine can tell those apart afterwards: a paused
session and an abandoned one leave the same dead process and the same untouched
files. `git tidy`'s worktree report reads this record, so a pause you write down
here is a pause it can see.

Run this with `retire` when the work is shipped and the checkout is disposable,
or `hold` with a reason when the thread is pausing and means to come back.
**Skip it entirely if you are unsure**: an absent disposition leaves the report
on its own evidence, which is the safe default.

```bash
python3 - <<'PY'
import json, os, subprocess
from datetime import datetime

DISPOSITION = "hold"                       # "hold" or "retire"
REASON      = "why this is paused, and what it is waiting on"   # "" for retire

CLAIMS = os.path.expanduser("~/.claude/session-claims/claims.json")
STORE  = os.path.expanduser("~/.claude/worktree-dispositions.json")
session = os.environ.get("CLAUDE_CODE_SESSION_ID")

checkout = None
try:
    # Same resolution as above: existing directories only, newest wins.
    mine = [r for r in json.load(open(CLAIMS))
            if r.get("session_id") == session and r.get("checkout")
            and os.path.isdir(r["checkout"])]
    if mine:
        checkout = max(mine, key=lambda r: r.get("claimed_at") or "")["checkout"]
except Exception:
    pass
if not checkout:
    checkout = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True).stdout.strip()
if not checkout or not os.path.isdir(checkout):
    raise SystemExit("no checkout to record a disposition for")

try:
    store = json.load(open(STORE))
    if not isinstance(store, dict):
        store = {}
except Exception:
    store = {}

store[os.path.realpath(checkout)] = {
    "disposition": DISPOSITION,
    "reason": REASON,
    "branch": subprocess.run(["git", "-C", checkout, "rev-parse",
                              "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True).stdout.strip(),
    "recorded_at": datetime.now().isoformat(timespec="seconds"),
    "session_id": session or "unknown",
}
os.makedirs(os.path.dirname(STORE), exist_ok=True)
with open(STORE, "w") as handle:
    json.dump(store, handle, indent=2)
print("recorded %s for %s" % (DISPOSITION, checkout))
PY
```

A declaration can only ever make the report **more** conservative. `hold`
promotes an otherwise-removable worktree to held, because a paused session's
checkout may hold no data and still break the cwd it resumes into. `retire`
never demotes: it does not outrank commits that are demonstrably not on the
default branch, and when the two disagree the report says so. That
disagreement means work landed after you thought you were done, which is worth
seeing.
