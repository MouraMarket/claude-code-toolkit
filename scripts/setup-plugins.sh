#!/usr/bin/env bash
#
# Add the recommended plugin marketplaces and install the recommended plugins,
# as listed in claude/recommended-plugins.json.
#
#   ./scripts/setup-plugins.sh          apply
#   ./scripts/setup-plugins.sh --check  list what it would do
#
# Plugins are a Claude Code concept, not a shell one, so this drives the `claude`
# CLI. It is idempotent — `claude plugin install` on an already-installed plugin
# is a no-op — and it never removes or disables anything you already have.
#
#   SETUP_PLUGINS_CATALOG=<file>  read a different list (the tests use this).
#
set -euo pipefail

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

ok()   { printf '  \033[32m%s\033[0m %s\n' "OK" "$*"; }
act()  { printf '  \033[33m%s\033[0m %s\n' "->" "$*"; }
warn() { printf '  \033[31m%s\033[0m %s\n' "!!" "$*"; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CATALOG="${SETUP_PLUGINS_CATALOG:-$REPO/claude/recommended-plugins.json}"

# The catalog, flattened to one line per entry: `M|name|source`, `A|name` for a
# marketplace whose background updates this turns on, `P|tier|id|why`. A
# catalog that cannot be read or holds a malformed entry stops the run: reading
# it as "nothing to install" would report success having checked nothing.
CATALOG_LINES="$(python3 - "$CATALOG" <<'PY'
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError) as exc:
    sys.exit("cannot read %s: %s" % (sys.argv[1], exc))


def text(value, field):
    if not isinstance(value, str) or not value or "|" in value or "\n" in value:
        sys.exit("%s: %s must be a non-empty string without '|' or a newline"
                 % (sys.argv[1], field))
    return value


markets, plugins = data.get("marketplaces"), data.get("plugins")
if not isinstance(markets, list) or not isinstance(plugins, list):
    sys.exit("%s: marketplaces and plugins must be lists" % sys.argv[1])
for entry in markets:
    if not isinstance(entry, dict) or not isinstance(entry.get("auto_update", False), bool):
        sys.exit("%s: a marketplace entry is malformed" % sys.argv[1])
    name = text(entry.get("name"), "marketplace name")
    print("M|%s|%s" % (name, text(entry.get("source"), "marketplace source")))
    if entry.get("auto_update"):
        print("A|%s" % name)
for entry in plugins:
    if not isinstance(entry, dict) or entry.get("tier") not in ("required", "optional"):
        sys.exit("%s: a plugin entry needs tier required or optional" % sys.argv[1])
    spec = text(entry.get("id"), "plugin id")
    if spec.count("@") != 1:
        sys.exit("%s: plugin id %s is not plugin@marketplace" % (sys.argv[1], spec))
    print("P|%s|%s|%s" % (entry["tier"], spec, text(entry.get("why"), "plugin why")))
PY
)" || { warn "the plugin list is unreadable, so nothing was checked or installed."; exit 1; }

#   name|source
MARKETPLACES=()
# Each entry: tier|plugin@marketplace|why it is here
PLUGINS=()
AUTO_UPDATE=()
while IFS= read -r line; do
  case "$line" in
    M\|*) MARKETPLACES+=("${line#M|}") ;;
    A\|*) AUTO_UPDATE+=("${line#A|}") ;;
    P\|*) PLUGINS+=("${line#P|}") ;;
  esac
done <<< "$CATALOG_LINES"

echo "Claude Code plugins"
(( CHECK )) && echo "  --check: reporting only"
echo

if ! command -v claude >/dev/null 2>&1; then
  warn "the \`claude\` CLI is not on your PATH — install Claude Code first,"
  warn "   then re-run this script."
  exit 1
fi

# Read a `claude ... --json` listing into the named variable. Unknown is not
# healthy: a command that fails, or prints something that is not a JSON array,
# stops the run rather than being read as "nothing installed".
read_list() {  # read_list <varname> <claude args...>
  # stdout only: a notice the CLI prints on stderr must not corrupt the JSON.
  local var="$1" out rc=0; shift
  out="$(claude "$@" 2>/dev/null)" || rc=$?
  if (( rc != 0 )); then
    warn "\`claude $*\` failed (exit $rc), so the plugin state is unknown."
    exit 1
  fi
  if ! printf '%s' "$out" | python3 -c 'import json,sys; assert isinstance(json.load(sys.stdin), list)' 2>/dev/null; then
    warn "\`claude $*\` did not print a JSON list, so the plugin state is unknown:"
    printf '       %s\n' "$out"
    exit 1
  fi
  printf -v "$var" '%s' "$out"
}

# present-and-enabled | present-disabled | absent, for one plugin spec in a
# `plugin list --json` listing. Only a USER-scope entry counts: a project-scoped
# copy is not the user's, and the CLI lists it all the same.
# The whole `plugin@marketplace` id is compared by equality — the bare name is a
# prefix of other plugins' names (`skills` of `skills-dev`), and a substring
# or grep test reports one installed the moment the other is.
plugin_state() {  # plugin_state <listing-json> <spec>
  printf '%s' "$1" | python3 -c '
import json, sys
spec = sys.argv[1]
state = "absent"
for p in json.load(sys.stdin):
    if p.get("id") == spec and p.get("scope") == "user":
        state = "enabled" if p.get("enabled") is True else "disabled"
        break
print(state)' "$2"
}

# The user-scope `version` of one plugin spec, or empty when absent.
plugin_version() {  # plugin_version <listing-json> <spec>
  printf '%s' "$1" | python3 -c '
import json, sys
for p in json.load(sys.stdin):
    if p.get("id") == sys.argv[1] and p.get("scope") == "user":
        print(p.get("version") or "")
        break' "$2"
}

# A required plugin that is already installed is updated, not just reported:
# an existing install can sit on a bundle from before a rename or a removal,
# and "installed and enabled" says nothing about which version. The end-state
# listing below reports what actually changed.
update_required() {  # update_required <spec>
  if (( CHECK )); then
    printf '       would update to the latest version\n'
  elif ! claude plugin update "$1" --scope user </dev/null >/dev/null 2>&1; then
    warn "could not update $1 — it may be out of date"
  fi
}

has_marketplace() {  # has_marketplace <listing-json> <name>
  printf '%s' "$1" | python3 -c '
import json, sys
sys.exit(0 if any(m.get("name") == sys.argv[1] for m in json.load(sys.stdin)) else 1)' "$2"
}

echo "Marketplaces"
read_list MKT_JSON plugin marketplace list --json
for entry in ${MARKETPLACES[@]+"${MARKETPLACES[@]}"}; do
  name="${entry%%|*}"; source="${entry##*|}"
  if has_marketplace "$MKT_JSON" "$name"; then
    # Present is not current: auto-update has been seen to leave an existing
    # install hours behind, so refresh the catalog explicitly.
    if (( CHECK )); then
      ok "$name (would refresh from its source)"
    elif claude plugin marketplace update "$name" </dev/null >/dev/null 2>&1; then
      ok "$name (refreshed)"
    else
      warn "$name is added but could not be refreshed — plugins from it may be out of date"
    fi
  else
    act "add $name ($source)"
    if (( ! CHECK )); then
      if ! claude plugin marketplace add "$source" >/dev/null 2>&1; then
        warn "could not add $source — if it is a private repo, check that your"
        warn "   GitHub account has access, then re-run."
      fi
    fi
  fi
done

# Claude Code's background updater is OFF for every marketplace that isn't
# Anthropic's, and `marketplace add` doesn't turn it on
# (code.claude.com/docs/en/plugins/loading, "When auto-update runs"). Without
# this, plugins from a marketplace marked `auto_update` in the catalog stay at
# whatever version was first installed. The flag goes on the marketplace's
# extraKnownMarketplaces entry in user settings, which outranks
# known_marketplaces.json. An explicit `false` is the user's own choice and is
# left alone.
# The one resolver for Claude Code's config dir, so a CLAUDE_CONFIG_DIR install
# gets its ACTIVE settings written, not an unused ~/.claude/settings.json.
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd -P)"

# Prints one word: on | set | would | absent | user-off | unreadable.
ensure_auto_update() {  # ensure_auto_update <name> <github owner/repo> <present 0|1>
  CHECK="$CHECK" LIB="$LIB" python3 - "$@" <<'PY'
import json, os, sys, tempfile

sys.path.insert(0, os.environ["LIB"])
from claude_home import claude_home

name, repo, present = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
path = os.path.join(claude_home(), "settings.json")
try:
    with open(path, encoding="utf-8") as handle:
        settings = json.load(handle)
except FileNotFoundError:
    settings = {}
except (OSError, ValueError):
    print("unreadable"); sys.exit(0)
if not isinstance(settings, dict):
    print("unreadable"); sys.exit(0)
known = settings.get("extraKnownMarketplaces")
if known is not None and not isinstance(known, dict):
    print("unreadable"); sys.exit(0)
entry = (known or {}).get(name)
if isinstance(entry, dict) and entry.get("autoUpdate") is True:
    print("on"); sys.exit(0)
if isinstance(entry, dict) and entry.get("autoUpdate") is False:
    print("user-off"); sys.exit(0)
# The /plugin "auto-update" toggle saves its choice in known_marketplaces.json
# when settings don't declare the marketplace. Writing true to settings would
# outrank it, so an explicit false there is an opt-out too.
if not (isinstance(entry, dict) and "autoUpdate" in entry):
    try:
        with open(os.path.join(claude_home(), "plugins", "known_marketplaces.json"),
                  encoding="utf-8") as handle:
            registry = json.load(handle)
        if isinstance(registry, dict) and isinstance(registry.get(name), dict) \
                and registry[name].get("autoUpdate") is False:
            print("user-off"); sys.exit(0)
    except (OSError, ValueError):
        pass
if entry is None and not present:
    # Declaring a marketplace we could not add would only make Claude Code
    # retry the clone in the background on every start.
    print("absent"); sys.exit(0)
if os.environ["CHECK"] == "1":
    print("would"); sys.exit(0)
if not isinstance(entry, dict):
    entry = {"source": {"source": "github", "repo": repo}}
entry["autoUpdate"] = True
settings.setdefault("extraKnownMarketplaces", {})[name] = entry
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".settings.", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(settings, handle, indent=2)
    handle.write("\n")
if os.path.exists(path):
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
os.replace(tmp, path)
print("set")
PY
}

# Background updates of a private marketplace use whatever GitHub login git
# already holds, and never prompt: with none, they fail quietly and the old
# copy stays. HTTPS with a stored credential first (what `gh auth setup-git` or
# the macOS keychain gives), then SSH, the same two routes Claude Code tries.
# Prompting is disabled on every channel, so this can't hang or pop a dialog,
# and the credential itself goes to /dev/null.
# Each probe gets 10 seconds (a credential helper can hang despite every
# no-prompt flag), and macOS has no `timeout`, so Python runs them.
# Prints one word: yes | no | unknown.
github_login_stored() {
  python3 - <<'PY'
import os, signal, subprocess, sys

env = {k: v for k, v in os.environ.items() if k not in ("GIT_ASKPASS", "SSH_ASKPASS")}
env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")

def run(cmd, stdin):
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, start_new_session=True)
    try:
        out, _ = proc.communicate(stdin, timeout=10)
        return proc.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return None, ""

# HTTPS with a stored credential. Its output IS the credential: never printed.
rc, _ = run(["git", "-c", "core.askPass=", "credential", "fill"],
            b"protocol=https\nhost=github.com\n\n")
if rc == 0:
    print("yes"); sys.exit(0)
https_hung = rc is None
# SSH. `ssh -T` exits 1 even after a good login (GitHub gives no shell), so
# read the greeting, not the exit code.
rc, out = run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "git@github.com"], b"")
if "successfully authenticated" in out:
    print("yes")
elif https_hung or rc is None:
    print("unknown")
else:
    print("no")
PY
}

# Only when the catalog asks for background updates: with none, there is
# nothing for a stored login to serve, so the probe would only cost time.
if (( ${#AUTO_UPDATE[@]} > 0 )); then
echo
echo "Auto-update"
(( CHECK )) || read_list MKT_JSON plugin marketplace list --json
for name in "${AUTO_UPDATE[@]}"; do
  repo=""
  for entry in ${MARKETPLACES[@]+"${MARKETPLACES[@]}"}; do
    [[ "${entry%%|*}" == "$name" ]] && repo="${entry##*|}"
  done
  present=0; has_marketplace "$MKT_JSON" "$name" && present=1
  case "$(ensure_auto_update "$name" "$repo" "$present")" in
    on)        ok "$name updates itself in the background" ;;
    set)       act "turned on background updates for $name" ;;
    would)     act "would turn on background updates for $name" ;;
    absent)    warn "$name isn't added, so it can't update itself (see Marketplaces above)" ;;
    user-off)  warn "$name has background updates turned off in your settings — left as you set it;"
               warn "   re-run this script now and then to stay current" ;;
    *)         warn "~/.claude/settings.json couldn't be read, so background updates for $name"
               warn "   were not turned on. Fix the file, then re-run this script." ;;
  esac
done
case "$(github_login_stored)" in
  yes) ;;
  no)  warn "no stored GitHub login found, so background updates of ${AUTO_UPDATE[*]}"
       warn "   can fail without telling you. Fix: gh auth login, then gh auth setup-git" ;;
  *)   warn "couldn't tell whether git has a stored GitHub login (the check timed out)."
       warn "   If plugins stop updating: gh auth login, then gh auth setup-git" ;;
esac
fi

echo
echo "Plugins"
# Ask once, not once per plugin — `claude plugin list` is slow enough to notice.
read_list PLUGIN_JSON plugin list --json
for entry in ${PLUGINS[@]+"${PLUGINS[@]}"}; do
  tier="${entry%%|*}"; rest="${entry#*|}"
  spec="${rest%%|*}"; why="${rest#*|}"
  case "$(plugin_state "$PLUGIN_JSON" "$spec")" in
    enabled)
      ok "$spec"
      if [[ "$tier" == required ]]; then update_required "$spec"; fi ;;
    disabled)
      if [[ "$tier" == required ]]; then
        act "enable $spec"
        (( CHECK )) || claude plugin enable "$spec" --scope user >/dev/null 2>&1 || true
        update_required "$spec"
      else
        warn "$spec is installed but disabled — left as you set it"
      fi ;;
    *)
      act "install $spec"
      printf '       %s\n' "$why"
      (( CHECK )) || claude plugin install "$spec" --scope user >/dev/null 2>&1 || true ;;
  esac
done

# Judge the END state, never the install/enable exit codes: a call can exit 0
# and change nothing, or fail after half-applying.
BEFORE_JSON="$PLUGIN_JSON"
(( CHECK )) || read_list PLUGIN_JSON plugin list --json
REQUIRED_FAILED=0; OPTIONAL_MISSING=0; FAILED_LINES=""
for entry in ${PLUGINS[@]+"${PLUGINS[@]}"}; do
  tier="${entry%%|*}"; rest="${entry#*|}"; spec="${rest%%|*}"
  state="$(plugin_state "$PLUGIN_JSON" "$spec")"
  if [[ "$tier" == required && "$state" != absent ]]; then
    was="$(plugin_version "$BEFORE_JSON" "$spec")"; now="$(plugin_version "$PLUGIN_JSON" "$spec")"
    if [[ -n "$was" && "$was" != "$now" ]]; then
      act "$spec updated ${was} -> ${now:-unknown}"
    fi
  fi
  if [[ "$tier" == required && "$state" != enabled ]]; then
    REQUIRED_FAILED=$((REQUIRED_FAILED + 1))
    FAILED_LINES="${FAILED_LINES}${spec} is not installed and enabled at user scope"$'\n'
  elif [[ "$tier" == optional && "$state" == absent ]]; then
    OPTIONAL_MISSING=$((OPTIONAL_MISSING + 1))
    (( CHECK )) || warn "$spec is optional, not installed"
  fi
done

echo
echo "Not installed by default, on purpose:"
echo "  Every enabled plugin costs context in EVERY session, before you use it."
echo "  Check any of them with: claude plugin details <name>@<marketplace>"
echo
echo "  plugin-dev@claude-plugins-official — skill-reviewer and plugin-validator,"
echo "    worth running before you publish a skill or plugin. Left out of the"
echo "    default because its descriptions add always-on context to every"
echo "    session, to support work most people do occasionally. Install it if you"
echo "    author skills regularly."
echo
echo "  skill-creator@claude-plugins-official — retired. Our skill-builder skill"
echo "    replaced it (it ships in the maintainer profile), and keeping both makes the two compete"
echo "    for the same requests. This script never uninstalls, so if you have it:"
echo "    claude plugin uninstall skill-creator@claude-plugins-official"
echo
echo "  github@claude-plugins-official — overlaps the \`gh\` CLI, which this toolkit"
echo "    uses directly. Install it if you prefer the plugin's workflow."
echo
echo "  code-review, code-simplifier, feature-dev@claude-plugins-official —"
echo "    all three duplicate something Claude Code already ships. \`/code-review\`"
echo "    and \`/simplify\` are built-in skills, and the built-in review takes an"
echo "    effort level and can post or apply its findings, which the plugin cannot."
echo "    Installing the plugin also makes \`/code-review\` ambiguous between the two."
echo "    feature-dev runs its own architect/executor process on Sonnet, which"
echo "    competes with /build rather than adding to it."
echo
echo "  pr-review-toolkit@claude-plugins-official — six review agents that overlap"
echo "    our own code-reviewer and task-reviewer. Worth installing if you want"
echo "    silent-failure-hunter or pr-test-analyzer specifically; skip it otherwise."
echo
echo "  coderabbit@claude-plugins-official — third-party review service. It sends"
echo "    your diff off-machine and needs its own CLI and account, so it is a data"
echo "    decision, not a plugin toggle. Do not install it for code you may not"
echo "    share with a third party."
echo
echo "  security-guidance@claude-plugins-official — genuinely useful, but it runs"
echo "    an LLM review at the end of EVERY turn, billed to you and invisible to"
echo "    claude-build-cost. If you install it, set these first:"
echo "      ENABLE_STOP_REVIEW=0   keeps the commit and push reviews, drops the"
echo "                             per-turn one. Its own README recommends this for"
echo "                             worktree and multi-agent setups, which this toolkit uses —"
echo "                             another session can move HEAD between your turns,"
echo "                             so the per-turn review reads someone else's diff."
echo "      SECURITY_GUIDANCE_DISABLE=1  full kill switch if it gets noisy."
echo "    Claude Code also ships a /security-review skill that costs nothing until"
echo "    you run it. Try that before adding a hook to every turn."
echo
if (( REQUIRED_FAILED > 0 )); then
  while IFS= read -r line; do [[ -n "$line" ]] && warn "$line"; done <<< "$FAILED_LINES"
  echo
  echo "Not done: $REQUIRED_FAILED required plugin(s) are not installed and enabled."
  exit 1
fi
if (( CHECK )); then
  echo "Check complete. Re-run without --check to apply."
else
  echo "Done. Restart Claude Code, then run /plugin to confirm what is enabled."
  (( OPTIONAL_MISSING > 0 )) && echo "$OPTIONAL_MISSING optional plugin(s) not installed, listed above."
fi
exit 0
