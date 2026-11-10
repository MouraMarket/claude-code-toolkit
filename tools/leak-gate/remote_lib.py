"""Remote audit: prove what a published remote actually exposes is clean.

The history gate proves a local repository before a push. This gate proves the
remote after it: every ref the host advertises, including pull-request refs
that a third party can create without the owner pushing anything, compared
exactly with the refs the owner meant to publish, then fetched into an
isolated audit repository and put through the same history gate.

Exit contract, as the other gates: 0 complete clean coverage, 1 findings,
2 refused.

What one run does:

1. **Enumerate** every advertised ref with `git ls-remote` (anonymous by
   default: what a stranger sees).
2. **Cross-check pull requests** against the host's own list. For a GitHub
   remote that is the API (`gh api .../pulls?state=all`); every listed pull
   request must have an advertised `refs/pull/N/head`, and every advertised
   `refs/pull/N/*` must belong to a listed pull request. Either gap refuses: a
   ref the host lists but will not serve cannot be scanned.
3. **Compare** the advertised refs with the expected publication refs, by name
   and object id. An unexpected, missing or moved ref is a finding.
4. **Fetch** every advertised ref into a fresh, isolated bare repository, and
   refuse unless the fetched refs equal the advertised ones exactly.
5. **Run the history gate** on that repository: every object, path, message
   and identity reachable from any advertised ref.

What it cannot establish (also in the receipt's limits): a host can keep
objects it does not advertise, and a ref created after step 1 is not covered.
"""

import collections
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse

import history_lib as hist
import leak_gate_lib as lib
from leak_gate_lib import Finding, Refused

NETWORK_TIMEOUT = 600
SCP_LIKE = re.compile(r"(?:[^@/:]+@)?([^/:]+):(?!//)(.*)\Z")
REPO_PART = re.compile(r"[A-Za-z0-9._-]+\Z")
PULL_REF = re.compile(r"refs/pull/(\d+)/(head|merge)\Z")
AUDIT_HEAD = "refs/remote-audit/unborn"

LIMITS = hist.LIMITS + (
    "a host can keep objects it does not advertise (deleted branches, cached "
    "pull-request data); only advertised refs are audited",
    "a ref changed between the two enumerations refuses; one changed after the "
    "second is not covered, so re-run after any change",
)


def github_repo(url):
    """(owner, repo) for a GitHub remote URL, else None. Refuses a GitHub URL it
    cannot read, so no spelling of a GitHub remote can skip the API check.

    Any form git accepts counts: scheme URLs with any case, user, port or
    trailing slash, and scp-like `user@host:owner/repo`. A host of github.com or
    any subdomain of it (ssh.github.com for SSH over 443) is GitHub.
    """
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        host, path = (parts.hostname or ""), parts.path
    else:
        m = SCP_LIKE.match(url)
        host, path = (m.group(1).lower(), m.group(2)) if m else ("", "")
    if host == "github.com" or host.endswith(".github.com"):
        segments = [s for s in path.split("/") if s]
        if len(segments) == 2:
            owner, repo = segments[0], re.sub(r"\.git\Z", "", segments[1])
            if REPO_PART.match(owner) and REPO_PART.match(repo):
                return owner, repo
        raise Refused("a GitHub remote whose owner and repository cannot be read")
    if "github.com" in url.lower():
        # A backstop: whatever this is, it names GitHub, so it is not waived.
        raise Refused("the remote names github.com in a form this gate cannot read")
    return None


def network_env(authenticated):
    """Anonymous by default: no global or system config, so no credentials.

    Authenticated keeps the user's git config, for a private remote, but still
    clears every inherited GIT_* variable.
    """
    env = hist.git_env()
    if authenticated:
        env.pop("GIT_CONFIG_NOSYSTEM", None)
        env.pop("GIT_CONFIG_GLOBAL", None)
    return env


def _net(args, env, what, cwd=None):
    try:
        proc = subprocess.run(["git"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                              timeout=NETWORK_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused("%s timed out" % what)
    except OSError:
        raise Refused("git cannot run here")
    if proc.returncode != 0:
        # stderr is not echoed: it can quote the URL. Exit status is enough.
        raise Refused("%s failed (exit %d): the remote is unreachable or not readable "
                      "with these credentials" % (what, proc.returncode))
    return proc.stdout


def advertised_refs(url, env):
    """{ref name: oid} for every advertised ref; peeled `^{}` lines are skipped."""
    out = _net(["ls-remote", "--", url], env, "git ls-remote")
    refs = {}
    for line in out.split(b"\n"):
        if not line:
            continue
        oid, sep, name = line.partition(b"\t")
        if not sep or not re.match(rb"[0-9a-f]{40}([0-9a-f]{24})?\Z", oid):
            raise Refused("git ls-remote returned a line this gate cannot read")
        if name.endswith(b"^{}"):
            continue
        refs[name.decode("utf-8", "surrogateescape")] = oid.decode()
    if not refs:
        raise Refused("the remote advertises no refs; an empty scope proves nothing")
    return refs


def github_pulls(owner, repo):
    """Every pull request number the GitHub API lists, any state."""
    try:
        proc = subprocess.run(
            ["gh", "api", "--paginate", "repos/%s/%s/pulls?state=all&per_page=100" % (owner, repo),
             "--jq", ".[].number"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            timeout=NETWORK_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("the GitHub pull-request list could not be read (gh)")
    if proc.returncode != 0:
        raise Refused("the GitHub pull-request list could not be read (gh exit %d)"
                      % proc.returncode)
    try:
        return {int(x) for x in proc.stdout.split()}
    except ValueError:
        raise Refused("the GitHub pull-request list could not be parsed")


def load_pulls(path):
    _raw, data = lib._read_json(path, "pull-request list")
    numbers = data.get("pull_requests") if isinstance(data, dict) else None
    if not isinstance(numbers, list) or not all(
            isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in numbers):
        raise Refused('the pull-request list must be {"pull_requests": [numbers]}')
    return set(numbers)


def check_pulls(advertised, pulls):
    """Refuse unless the host's pull-request list and its pull refs agree."""
    heads, listed_refs = set(), set()
    for name in advertised:
        m = PULL_REF.match(name)
        if m:
            listed_refs.add(int(m.group(1)))
            if m.group(2) == "head":
                heads.add(int(m.group(1)))
        elif name.startswith("refs/pull/"):
            raise Refused("the remote advertises a pull ref of an unknown shape")
    missing = sorted(pulls - heads)
    if missing:
        raise Refused("%d pull request(s) the host lists have no advertised head ref, "
                      "so their content cannot be fetched or scanned" % len(missing))
    unlisted = sorted(listed_refs - pulls)
    if unlisted:
        raise Refused("%d advertised pull ref(s) belong to no listed pull request: "
                      "the pull-request list is incomplete" % len(unlisted))


def load_expected(path):
    """{ref name: oid} the owner intends to publish."""
    _raw, data = lib._read_json(path, "expected refs")
    if not isinstance(data, dict) or data.get("schema_version") != lib.SCHEMA_VERSION:
        raise Refused("the expected refs are not schema_version %d" % lib.SCHEMA_VERSION)
    refs = data.get("refs")
    if not isinstance(refs, dict) or not refs:
        raise Refused("the expected refs list nothing; an empty expectation proves nothing")
    for name, oid in refs.items():
        if not isinstance(oid, str) or not re.match(r"[0-9a-f]{40}([0-9a-f]{24})?\Z", oid):
            raise Refused("an expected ref has no full object id")
        if name != "HEAD" and not name.startswith("refs/"):
            raise Refused("an expected ref name is neither HEAD nor under refs/")
    return refs


def compare_refs(advertised, expected):
    """Findings for every advertised ref that is not exactly what was expected."""
    out = []
    for name in sorted(set(advertised) - set(expected)):
        out.append(("unexpected-ref", name, "advertised but not an expected publication ref"))
    for name in sorted(set(expected) - set(advertised)):
        out.append(("missing-ref", name, "expected but not advertised"))
    for name in sorted(set(advertised) & set(expected)):
        if advertised[name] != expected[name]:
            out.append(("moved-ref", name, "advertised at a different object than expected"))
    return [Finding(cls, "remote", name.encode("utf-8", "surrogateescape"), None, None, None,
                    detail, None) for cls, name, detail in out]


def fetch_all(url, advertised, audit, env):
    """Fetch every advertised ref into the fresh bare repository `audit`."""
    _net(["init", "-q", "--bare", audit], env, "git init")
    _net(["-C", audit, "symbolic-ref", "HEAD", AUDIT_HEAD], env, "git symbolic-ref")
    _net(["-C", audit, "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--", url,
          "+refs/*:refs/*"], env, "git fetch")
    out = _net(["-C", audit, "for-each-ref", "--format=%(objectname) %(refname)"], env,
               "git for-each-ref")
    fetched = {}
    for line in out.decode().splitlines():
        oid, _sep, name = line.partition(" ")
        fetched[name] = oid
    wanted = {k: v for k, v in advertised.items() if k.startswith("refs/")}
    if fetched != wanted:
        raise Refused("the fetch brought %d refs where %d were advertised, or at other "
                      "objects: a partial fetch" % (len(fetched), len(wanted)))


def run_remote_audit(url, expected_path, profile_path, identity, exceptions_path=None,
                     pulls_path=None, no_pull_requests=False, authenticated=False,
                     names_path=lib.DEFAULT_NAMES, words_path=lib.DEFAULT_WORDS):
    """Run the remote audit. Raises Refused.

    Returns (exit code, findings, report lines, summary), as the other gates do.
    """
    hist.parse_identity(identity)
    profile = lib.load_profile(profile_path)
    try:
        return _run(url, expected_path, profile_path, profile, identity, exceptions_path,
                    pulls_path, no_pull_requests, authenticated, names_path, words_path)
    except Refused as exc:
        raise Refused(lib.scrub(str(exc), profile, None))


def _run(url, expected_path, profile_path, profile, identity, exceptions_path, pulls_path,
         no_pull_requests, authenticated, names_path, words_path):
    expected = load_expected(expected_path)
    gh = github_repo(url)
    sources = sum(1 for s in (gh is not None, pulls_path is not None, no_pull_requests) if s)
    if gh is not None and (pulls_path is not None or no_pull_requests):
        raise Refused("a GitHub remote's pull requests come from the GitHub API, never "
                      "from a file or a waiver")
    if sources != 1:
        raise Refused("name exactly one pull-request source: a GitHub remote, "
                      "--pulls FILE, or --no-pull-requests for a host without them")

    env = network_env(authenticated)
    if authenticated:
        # The user's config can rewrite URLs (insteadOf); the audit must reach
        # the remote it names, not wherever a rewrite sends it.
        resolved = _net(["ls-remote", "--get-url", "--", url], env, "git ls-remote --get-url")
        if resolved.strip().decode("utf-8", "replace") != url:
            raise Refused("git config rewrites this URL to another remote")

    advertised = advertised_refs(url, env)
    if gh is not None:
        pulls = github_pulls(*gh)
    elif pulls_path is not None:
        pulls = load_pulls(pulls_path)
    else:
        pulls = set()
    check_pulls(advertised, pulls)
    ref_findings = compare_refs(advertised, expected)

    base = tempfile.mkdtemp(prefix="leak-gate-audit-")
    try:
        audit = os.path.join(base, "audit.git")
        fetch_all(url, advertised, audit, env)
        # The remote must not have changed while it was being fetched: HEAD
        # moving to another advertised branch would leave every fetched ref
        # matching while the published default changed.
        if advertised_refs(url, env) != advertised:
            raise Refused("the remote's refs changed during the audit; re-run it")
        code, findings, _lines, history = hist.run_history(
            audit, profile_path, identity, exceptions_path, names_path=names_path,
            words_path=words_path)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    lists = lib.load_name_lists(names_path, words_path)
    kept = sorted(ref_findings + findings, key=lib._sort_key)
    ref_lines = sorted("%s %s" % (k, v) for k, v in advertised.items())
    summary = {
        "gate": "remote-audit",
        "schema_version": lib.SCHEMA_VERSION,
        "exit": 1 if kept else 0,
        "remote_sha256": hashlib.sha256(url.encode()).hexdigest(),
        "access": "authenticated" if authenticated else "anonymous",
        "advertised_refs": len(advertised),
        "advertised_sha256": hashlib.sha256("\n".join(ref_lines).encode()).hexdigest(),
        "pull_requests": len(pulls),
        "pull_request_source": ("github-api" if gh else "file" if pulls_path
                                else "none (waived)"),
        "ref_findings": len(ref_findings),
        "history": {k: v for k, v in history.items() if k not in ("findings", "limits")},
        "findings_by_class": dict(sorted(collections.Counter(f.cls for f in kept).items())),
        "findings": [{k: lib.scrub(v, profile, lists) if isinstance(v, str) else v
                      for k, v in lib.receipt_finding(f, profile, lists).items()}
                     for f in kept],
        "limits": list(LIMITS),
    }
    lines = [lib.scrub(lib.format_finding(f, profile, lists), profile, lists) for f in kept]
    return summary["exit"], kept, lines, summary
