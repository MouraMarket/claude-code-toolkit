"""Shared fixtures for tools/setup-audit's suites.

Never touch the real `$HOME`: every sandbox is a fresh tempdir, and
`env_for` -- the one place a subprocess environment gets built -- asserts its
`HOME` differs from the real one, captured at import before any override.
This mirrors `lib/tests/test_install_manifest.py`'s own `env_for`, which
predates `audit_lib` and could not import its constant yet; this one reuses
`audit_lib.GUARD_ESCAPE_HATCHES` directly rather than keeping a second copy.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))
CLI = os.path.join(TOOL_DIR, "claude-setup-audit")

if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

# `audit_lib` imports this at load, for `unrunnable_hooks`.
INTEGRITY_LIB = os.path.join("tools", "toolkit-integrity", "toolkit_integrity_lib.py")

import audit_lib  # noqa: E402

# The shared `claude` CLI shim every install.sh suite uses, loaded by path so
# a fixture install never reaches the real CLI on this machine's PATH.
sys.path.insert(0, os.path.join(REPO, "lib", "tests"))
import install_fixture as shim_fx  # noqa: E402

# Captured at import, before any test ever overrides HOME.
REAL_HOME = os.path.expanduser("~")


def sandbox_home():
    return tempfile.mkdtemp(prefix="setup-audit-test-")


def env_for(home, extra=None):
    """A hermetic subprocess environment: start from a copy of
    `os.environ`, strip every `GIT_*` var plus `XDG_CONFIG_HOME` and the guard
    escape hatches, then point `HOME` and `CLAUDE_TOOLKIT_CONFIG` into the
    sandbox. `extra` is applied last, never inherited ambiently -- a test that
    exercises an escape hatch passes it through `extra`."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_") or key == "XDG_CONFIG_HOME" \
           or key in audit_lib.GUARD_ESCAPE_HATCHES:
            del env[key]
    env["HOME"] = home
    env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(
        home, ".config", "claude-code-toolkit", "config.json")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if extra:
        env.update(extra)
    assert env["HOME"] != REAL_HOME
    return env


def write(path, text):
    """Create `path`'s parent directories and write `text` to it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


def run_cli(args, home, extra_env=None):
    """Run the real `claude-setup-audit` executable. Returns
    `(returncode, stdout, stderr)`."""
    proc = subprocess.run(
        [CLI] + list(args), capture_output=True, text=True,
        env=env_for(home, extra_env), cwd=REPO,
        stdin=subprocess.DEVNULL, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def _git(*args, cwd):
    """A fixture-only git invocation, isolated from the machine's own git
    config -- mirrors the identical helper in lib/tests/test_install_*.py.
    Without this, a fixture commit runs whatever hook the real machine's
    global core.hooksPath points at (this repo's own secret-scan, on a
    machine that has run scripts/setup-git.sh), which would make building a
    fixture depend on something installed rather than on itself."""
    env = {key: value for key, value in os.environ.items()
           if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                          "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, env=env, universal_newlines=True,
                          stdin=subprocess.DEVNULL)


def require_own_git_dir(repo):
    """Every fixture git call after `init` writes. Refuse unless git
    resolves `repo`'s OWN `.git`, never a repository it reached through a
    link or a `gitdir:` file."""
    git_dir = _git("rev-parse", "--absolute-git-dir", cwd=repo).stdout.strip()
    assert os.path.realpath(git_dir) == os.path.realpath(os.path.join(repo, ".git")), \
        "fixture git resolves %r, not its own .git" % (git_dir,)


def git_worktree_add(repo, path, branch="wt"):
    """A real linked worktree of fixture `repo` at `path`."""
    _git("worktree", "add", path, "-b", branch, cwd=repo)


def _mirror(repo, target):
    """A symlink at the same relative path under `repo`, pointing at the
    REAL file `target` (which must be under `REPO`) -- the code a fixture
    run exercises is the actual repo content, never a copy.

    Never anything named `.git*` at the top: in a linked worktree `.git` is
    a FILE pointing at the real repository, and a fixture carrying a link to
    it runs its `git init`/`config`/`commit` against the real repository's
    shared config and the real branch."""
    rel = os.path.relpath(target, REPO)
    assert not rel.startswith(".."), "target %r is not under REPO" % (target,)
    assert not rel.split(os.sep)[0].startswith(".git"), \
        "refusing to mirror %r into a fixture" % (rel,)
    dest = os.path.join(repo, rel)
    if os.path.lexists(dest):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.symlink(target, dest)
    return dest


def _mirrored_files():
    """Every real file a fixture install can reach, and no test: the files at
    the repo root, everything under `lib/`, `scripts/` and `claude/` outside
    a `tests/` directory, the files at the top of each `tools/<name>/`, the
    marketplace's `.claude-plugin/`, and each plugin's two manifests
    (`.claude-plugin/` and `hooks/hooks.json`)."""
    out = [os.path.join(REPO, name) for name in sorted(os.listdir(REPO))
           if os.path.isfile(os.path.join(REPO, name)) and not name.startswith(".git")]
    for top in ("lib", "scripts", "claude"):
        for base, dirs, files in os.walk(os.path.join(REPO, top)):
            dirs[:] = sorted(d for d in dirs if d not in ("tests", "__pycache__"))
            out.extend(os.path.join(base, name) for name in sorted(files))
    tools = os.path.join(REPO, "tools")
    for tool in sorted(os.listdir(tools)):
        tool_dir = os.path.join(tools, tool)
        if os.path.isdir(tool_dir):
            out.extend(os.path.join(tool_dir, name) for name in sorted(os.listdir(tool_dir))
                       if os.path.isfile(os.path.join(tool_dir, name)))
    for name in [""] + sorted(os.listdir(REPO)):
        for manifest in ((".claude-plugin",), ("hooks", "hooks.json")):
            path = os.path.join(REPO, name, *manifest)
            if os.path.isdir(path):
                out.extend(os.path.join(path, f) for f in sorted(os.listdir(path)))
            elif os.path.isfile(path):
                out.append(path)
    return [path for path in out if not path.endswith(".pyc")]


def make_fixture_repo(root, name="fixture"):
    """A git repo laid out like the toolkit, but with NO `tests/` directory
    anywhere -- so a real `install.sh` run against it finds no suite to run
    and never recurses into this suite itself.

    Everything in it is a symlink to the REAL file at the same relative
    path, never a copy, so a real `install.sh` / `scripts/setup-git.sh` run
    against it exercises the actual repo content. Symlinking a whole
    `tools/<name>/` directory would carry its own `tests/` along, which is
    exactly what this fixture must not have, so files are mirrored one by
    one by a rule rather than a list: every file `_mirrored_files` yields.
    A list of the files the installer reads went stale as the installer
    grew; the rule cannot, because it never names a file.
    """
    repo = os.path.join(root, name)
    os.makedirs(repo)

    for path in _mirrored_files():
        _mirror(repo, path)
    with open(os.path.join(REPO, "claude", "profiles.json"), encoding="utf-8") as handle:
        extra_dirs = [(e["dir"], e["agents"]) for k, e in json.load(handle)["profiles"].items()
                      if k != "//" and e["dir"] != "plugin"]

    # The plugin's own relative symlinks, resolved inside the fixture so a
    # registration's `plugin/tools/<dir>/<exe>` reaches the mirrored tool.
    os.symlink(os.path.join("..", "tools"), os.path.join(repo, "plugin", "tools"))
    os.symlink(os.path.join("..", "lib"), os.path.join(repo, "plugin", "lib"))

    # What `install.sh`'s plugin step requires of a checkout: the resolver, and
    # `skills/` and `agents/` reached through relative symlinks. Replicates
    # `lib/tests/install_fixture.add_plugin_layout`'s steps rather than calling
    # it, because that helper writes JSON through paths this fixture has
    # mirrored as symlinks to the REAL files. A `.keep` holds each directory
    # in the fixture's git commit.
    plugin_paths_copy = os.path.join(repo, "lib", "plugin_paths.py")
    if os.path.lexists(plugin_paths_copy):
        os.remove(plugin_paths_copy)
    shutil.copyfile(os.path.join(REPO, "lib", "plugin_paths.py"), plugin_paths_copy)
    for component in ("skills", "agents"):
        os.makedirs(os.path.join(repo, component), exist_ok=True)
        open(os.path.join(repo, component, ".keep"), "w").close()
        os.symlink(os.path.join("..", component), os.path.join(repo, "plugin", component))
    for directory, agents in extra_dirs:
        os.symlink(os.path.join("..", "skills"), os.path.join(repo, directory, "skills"))
        os.symlink(os.path.join("..", "tools"), os.path.join(repo, directory, "tools"))
        os.symlink(os.path.join("..", "lib"), os.path.join(repo, directory, "lib"))
        if agents:
            os.symlink(os.path.join("..", "agents"), os.path.join(repo, directory, "agents"))

    # Hook targets and plugin components later port rows supply, as stubs
    # inside the fixture only (the real install.sh stays strict about them).
    shim_fx.stub_pending_components(repo)

    _git("init", "-q", "-b", "main", repo, cwd=root)
    require_own_git_dir(repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def write_config(fixture, home, workspace_root):
    """The real, non-interactive `lib/setup_config.py --write`, run against
    the FIXTURE's own symlinked copy -- so a subsequent `install.sh`'s Config
    section finds a valid config and never reaches interactive first-time
    setup. Shared by the snapshot and apply-safe suites, both of which drive a real
    install against `make_fixture_repo`."""
    cfg = os.path.join(home, ".config", "claude-code-toolkit", "config.json")
    proc = subprocess.run(
        ["python3", os.path.join(fixture, "lib", "setup_config.py"),
         cfg, fixture, "--write", "--name", "T", "--workspace-root", workspace_root],
        env=env_for(home), cwd=fixture, capture_output=True,
        text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return cfg


def run_install(fixture, home):
    """The real `bash <fixture>/install.sh`, in a hermetic sandbox env."""
    proc = subprocess.run(
        ["bash", os.path.join(fixture, "install.sh")],
        # Shim `claude` first on PATH (never the real CLI), and skip the suite
        # sweep: the fixture has no tests/ by design and "found nothing" fails.
        env=shim_fx.shim_env_for_home(env_for(home), home, skip_suites=True),
        cwd=fixture, capture_output=True,
        text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc
