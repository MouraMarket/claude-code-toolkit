#!/usr/bin/env python3
"""Tests for claude-secret-scan.

Half of these are negative cases, on purpose. A secret scanner is judged on
what it does NOT flag: one false positive on ordinary work and the next person
adds --no-verify to muscle memory, at which point the true positives stop
mattering too.

Every credential in this file is synthetic — constructed to match a shape, never
copied from a real key.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
SCANNER = os.path.join(TOOL_DIR, "claude-secret-scan")
sys.path.insert(0, TOOL_DIR)
# The sibling linter this hook CHAINS to. Imported for its cap constant only, so
# `test_oversized_claude_md_blocks_the_commit` sizes its fixture off the real
# number instead of a literal that goes stale the moment the cap moves — which
# is exactly what happened when it was standardized to 800 on 2026-09-15.
sys.path.insert(0, os.path.join(os.path.dirname(TOOL_DIR), "context-lint"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(TOOL_DIR)), "lib"))

import secret_lib  # noqa: E402
import context_lint_lib  # noqa: E402


def _body(length, alphabet="A9f2Kd7Lp1QwZ3mR8vXb6NtJ5sG0yH4cE"):
    """A synthetic key body of an exact length.

    Built rather than typed: two of these were originally hand-written one
    character short of their pattern's minimum, and the suite read as "the
    google and github rules are broken" when the rules were fine and the
    fixtures were not. Length is the thing these patterns turn on, so it should
    not be something a person counts by eye.
    """
    repeats = (length // len(alphabet)) + 1
    return (alphabet * repeats)[:length]


# Synthetic values, each matching one vendor's documented shape. None is copied
# from a real credential.
#
# No literal in this file matches a rule. Each credential-shaped value is
# assembled when the test runs, so the file itself carries nothing the leak gate
# or this scanner would flag, and needs no inline allow marker or exception.
FAKE = {
    "anthropic-key": "sk-" + "ant-api03-" + _body(95),
    "google-api-key": "AIza" + _body(35),
    "google-oauth-secret": "GOCSPX-" + _body(28),
    "github-token": "ghp_" + _body(36),
    "github-pat": "github_pat_" + _body(60),
    "gitlab-token": "glpat-" + _body(20),
    "slack-token": "xox" + "b-298374652871-" + _body(24),
    "slack-app-token": "xapp-1-" + _body(20),
    "aws-access-key": "AKIA" + "J7QZ3MWK9LP2RTX4",
    "clickup-token": "pk_48291736_" + "K9WMZQ3LP7RTX4BVN2HJ8FGD5CY6SA1E",
    "stripe-key": "sk_live_" + _body(24),
    "npm-token": "npm_" + _body(36),
    "openai-key": "sk-" + "proj-" + _body(48),
    "generic-sk-key": "sk-" + _body(48),
}

# A PEM header is a fixed string, so split it where no rule can see it whole.
PEM_BEGIN = "-----" + "BEGIN "


class TestDetects(unittest.TestCase):
    """Each vendor shape is caught."""

    def test_each_pattern_fires(self):
        for rule, value in FAKE.items():
            with self.subTest(rule=rule):
                findings = secret_lib.scan_line("API_KEY = '%s'" % value)
                self.assertTrue(findings, "no finding for %s" % rule)
                self.assertEqual(findings[0].rule, rule)

    def test_every_rule_has_a_fixture(self):
        """A rule with no synthetic value is a rule nothing proves fires here.

        The table comes from the leak gate, so a rule added there reaches this
        scanner unannounced; this is what makes the new rule need a fixture.
        """
        rules = [name for name, _pattern in secret_lib.PATTERNS]
        self.assertEqual(sorted(set(FAKE) | {"private-key-block"}), sorted(rules))

    def test_aws_temporary_key_prefix(self):
        findings = secret_lib.scan_line("AWS_ACCESS_KEY_ID=" + "ASIA" + "J7QZ3MWK9LP2RTX4")
        self.assertEqual([f.rule for f in findings], ["aws-access-key"])

    def test_private_key_block(self):
        findings = secret_lib.scan_line(PEM_BEGIN + "PRIVATE KEY-----")
        self.assertEqual([f.rule for f in findings], ["private-key-block"])

    def test_openssh_and_rsa_variants(self):
        for kind in ("RSA", "OPENSSH", "EC"):
            header = PEM_BEGIN + kind + " PRIVATE KEY-----"
            with self.subTest(header=kind):
                self.assertTrue(secret_lib.scan_line(header))

    def test_hardcoded_key_assigned_into_environ_is_still_flagged(self):
        """The canonical way people hardcode a key, and a regression guard.

        An earlier version skipped any line matching an env-var/op:// "this is
        a reference, not a literal" shape. This line matches that shape AND
        carries a live credential, so it was silently allowed — the one hole
        that mattered. Mutation-testing found it: disabling the suppression
        changed no test outcome, which meant nothing actually covered it.
        """
        for line in ('os.environ["ANTHROPIC_API_KEY"] = "%s"' % FAKE["anthropic-key"],
                     "export ANTHROPIC_API_KEY=%s" % FAKE["anthropic-key"],
                     'key = os.getenv("K", "%s")' % FAKE["anthropic-key"],
                     'TOKEN = "op://Vault/Item/cred" or "%s"' % FAKE["github-token"]):
            with self.subTest(line=line[:40]):
                self.assertTrue(secret_lib.scan_line(line),
                                "a real credential was suppressed")

    def test_service_account_json_private_key(self):
        # A cloud service account's JSON key file carries its private key
        # inline, and one such grant can reach a lot of data.
        line = '  "private_key": "%sPRIVATE KEY-----\\nMIIEvg..."' % PEM_BEGIN
        self.assertTrue(secret_lib.scan_line(line))

    def test_finding_reports_file_and_line(self):
        text = "clean\nclean\nkey = '%s'\n" % FAKE["anthropic-key"]
        findings = secret_lib.scan_text(text, "config.py")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "config.py")
        self.assertEqual(findings[0].line_number, 3)


class TestNeverLeaksTheSecret(unittest.TestCase):
    """A scanner that prints what it finds has copied the credential somewhere new."""

    def test_redaction_omits_the_middle(self):
        value = FAKE["anthropic-key"]
        redacted = secret_lib.redact(value)
        self.assertNotIn(value, redacted)
        self.assertIn(str(len(value)), redacted)

    def test_formatted_finding_does_not_contain_the_secret(self):
        value = FAKE["github-token"]
        finding = secret_lib.scan_line("t = '%s'" % value, "a.py", 1)[0]
        self.assertNotIn(value, finding.format())

    def test_control_characters_in_a_path_cannot_forge_report_lines(self):
        """`--all` now reaches files named with a newline or tab; their findings
        must still print as one line each, naming the file unambiguously."""
        finding = secret_lib.Finding("new\nline\t.txt", 1, "anthropic-key", "sk-ant…xx")
        formatted = finding.format()
        self.assertNotIn("\n", formatted)
        self.assertNotIn("\t", formatted)
        self.assertIn("new\\nline\\t.txt:1", formatted)
        plain = secret_lib.Finding("café.txt", 3, "anthropic-key", "sk-ant…xx").format()
        self.assertIn("café.txt:3", plain)

    def test_report_output_does_not_contain_the_secret(self):
        """End to end: the process's stderr must not carry the credential."""
        value = FAKE["anthropic-key"]
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "leak.py")
            with open(target, "w") as handle:
                handle.write("KEY = '%s'\n" % value)
            result = subprocess.run(
                (sys.executable, SCANNER, target),
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(value, result.stdout + result.stderr)
        self.assertIn("anthropic-key", result.stderr)


class TestDoesNotMisfire(unittest.TestCase):
    """The half that decides whether anyone keeps this enabled."""

    def test_one_password_reference_is_the_correct_pattern(self):
        # This is how every credential in this codebase is supposed to be
        # written. Flagging it would be actively wrong.
        self.assertEqual(
            secret_lib.scan_line('TOKEN = "op://My Vault/Some API Token/credential"'), [])

    def test_env_var_references(self):
        for line in ('key = os.environ["ANTHROPIC_API_KEY"]',
                     'key = os.getenv("SERVICE_API_TOKEN")',
                     'token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}',
                     'rule: ${{ vars.ANTHROPIC_ORG_ID }}',
                     'export ANTHROPIC_API_KEY="$MY_KEY"'):
            with self.subTest(line=line):
                self.assertEqual(secret_lib.scan_line(line), [])

    def test_placeholder_filter_suppresses_a_matching_fake(self):
        """Lines a pattern DOES match, dropped by _looks_like_placeholder.

        Split from the shapes below deliberately. Both used to live in one test
        named for the filter, and two of the three never reached it — they were
        clean because no pattern matched, so the filter could have been deleted
        with the test still green. Every line here must match a pattern first,
        or it is not testing the filter.
        """
        cases = (
            # Only the xxxx branch catches this one: the body is character-diverse,
            # so the entropy branch does not fire. An all-x body would be caught by
            # both, which makes it useless for proving either.
            'ANTHROPIC_API_KEY=sk-' + 'ant-api03-xxxxAbCdEfGh1234JkLmNoPq',
            # Only the entropy branch catches this one: no "xxxx" anywhere.
            'ANTHROPIC_API_KEY=sk-' + 'ant-api03-aaaaaaaaaaaabbbbbbbbbbbb',
        )
        for line in cases:
            with self.subTest(line=line):
                self.assertTrue(
                    any(p.search(line) for _, p in secret_lib.PATTERNS),
                    "line must match a pattern or it does not exercise the filter")
                self.assertEqual(secret_lib.scan_line(line), [])

    def test_placeholder_shapes_never_match_a_pattern(self):
        """`<...>` and `...` docs placeholders, clean for a different reason.

        Not filtered — never matched. Every pattern is built from
        `[A-Za-z0-9_-]`-style classes, so `<`, `>` and `.` terminate the match
        and the remaining run is too short to qualify. Asserted explicitly so
        the reason stays visible: if a future pattern admits these characters,
        this fails and _looks_like_placeholder needs its branches back.
        """
        for line in ('key = "sk-ant-api03-<your-key-here-goes-in-this-slot>"',
                     'ANTHROPIC_API_KEY=sk-ant-api03-your-api-key...here'):
            with self.subTest(line=line):
                self.assertFalse(
                    any(p.search(line) for _, p in secret_lib.PATTERNS),
                    "a pattern now matches this — restore the < > and ... branches")
                self.assertEqual(secret_lib.scan_line(line), [])

    def test_ordinary_code_and_prose(self):
        for line in ("git checkout -b feat/sk-ant-support",
                     "commit 854829e7f3a1b2c4d5e6f7a8b9c0d1e2f3a4b5c6",
                     "https://github.com/mouramarket/claude-code-toolkit",
                     "the sk-ant- prefix identifies an Anthropic key",
                     "id = '550e8400-e29b-41d4-a716-446655440000'",
                     "AIzaSyD",  # too short to be a key
                     "pk_test_1234",
                     "def scan_all(): pass"):
            with self.subTest(line=line):
                self.assertEqual(secret_lib.scan_line(line), [])

    def test_base64_blob_is_not_flagged(self):
        # No prefix rule matches, which is exactly why entropy heuristics were
        # rejected — this would trip every one of them.
        blob = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        self.assertEqual(secret_lib.scan_line("data = '%s'" % blob), [])

    def test_allowlist_marker_suppresses(self):
        line = "EXAMPLE = '%s'  # allowlist secret" % FAKE["anthropic-key"]
        self.assertEqual(secret_lib.scan_line(line), [])

    def test_this_repo_is_clean(self):
        """The toolkit's own tracked files must not trip its own scanner.

        Catches a pattern written so loosely it flags the repo it ships in —
        which would be discovered by everyone at once, on their next commit.

        No line in this file carries an `allowlist secret` marker: every
        credential-shaped input is assembled at runtime, so the tree passes the
        leak gate too, which has no marker. A newly-too-loose pattern that flags
        anything in the repo fails here.

        Note `--all` reads `git ls-files`, so it only sees TRACKED files. This
        check passing on a working tree with new untracked files is not
        evidence about those files. The pre-commit hook scans the staged diff,
        which is the gate that actually matters.
        """
        result = subprocess.run(
            (sys.executable, SCANNER, "--all"),
            cwd=os.path.dirname(os.path.dirname(TOOL_DIR)),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0,
                         "scanner flags its own repo:\n%s" % result.stderr)


class TestDiffScanning(unittest.TestCase):
    """Only added lines, and the line numbers must point at the right place."""

    DIFF = """diff --git a/app.py b/app.py
index 1234567..89abcde 100644
--- a/app.py
+++ b/app.py
@@ -10,0 +11,2 @@ def configure():
+    KEY = "{key}"
+    other = 1
""".format(key=FAKE["anthropic-key"])

    def test_added_line_is_flagged_with_its_new_side_number(self):
        findings = secret_lib.scan_diff(self.DIFF)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "app.py")
        self.assertEqual(findings[0].line_number, 11)

    def test_removed_line_is_not_flagged(self):
        """Removing a secret must not be blocked by the secret being removed.

        Otherwise the only way to clean a file is --no-verify, and the check
        teaches people to bypass it during the exact commit it should welcome.
        """
        diff = self.DIFF.replace('+    KEY = "', '-    KEY = "')
        self.assertEqual(secret_lib.scan_diff(diff), [])

    def test_context_lines_are_not_flagged(self):
        diff = self.DIFF.replace('+    KEY = "', '     KEY = "')
        self.assertEqual(secret_lib.scan_diff(diff), [])

    def test_added_line_starting_with_plus_plus_is_flagged(self):
        """`+` then `++TOKEN=...` is an added line, not a `+++` file header.

        The old parser dropped every line starting `+++`, so a credential whose
        line began `++` was never scanned and the commit read as clean.
        """
        diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
                "@@ -0,0 +3 @@\n+++TOKEN=%s\n" % FAKE["anthropic-key"])
        findings = secret_lib.scan_diff(diff)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "app.py")
        self.assertEqual(findings[0].line_number, 3)

    def test_header_plus_plus_plus_still_sets_the_path(self):
        diff = ("diff --git a/one.py b/one.py\n--- a/one.py\n+++ b/one.py\n"
                "@@ -0,0 +1 @@\n+x = 1\n"
                "diff --git a/two.py b/two.py\n--- a/two.py\n+++ b/two.py\n"
                "@@ -0,0 +1 @@\n+KEY = \"%s\"\n" % FAKE["anthropic-key"])
        findings = secret_lib.scan_diff(diff)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "two.py")

    def test_unicode_line_separator_does_not_hide_an_added_line(self):
        """U+2028 splits a line under str.splitlines(), orphaning the tail
        without its `+` prefix, so the credential after it went unscanned."""
        diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
                "@@ -0,0 +1 @@\n+x = 1\u2028KEY = \"%s\"\n" % FAKE["anthropic-key"])
        self.assertEqual(len(secret_lib.scan_diff(diff)), 1)

    def test_no_newline_marker_does_not_shift_line_numbers(self):
        header = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        key_line = "+KEY = \"%s\"\n" % FAKE["anthropic-key"]
        marker = "\\ No newline at end of file\n"
        new_hunk = header + "@@ -0,0 +1 @@\n+a\n" + marker + "@@ -0,0 +5 @@\n" + key_line
        self.assertEqual(secret_lib.scan_diff(new_hunk)[0].line_number, 5)
        one_hunk = header + "@@ -0,0 +1,2 @@\n+a\n" + marker + key_line
        self.assertEqual(secret_lib.scan_diff(one_hunk)[0].line_number, 2)


class TestExitCodes(unittest.TestCase):

    def test_clean_paths_exit_zero(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "clean.py")
            with open(target, "w") as handle:
                handle.write("import os\nkey = os.environ['K']\n")
            result = subprocess.run((sys.executable, SCANNER, target),
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)

    def test_run_through_a_symlink_still_loads_its_rules(self):
        """~/.local/bin/claude-secret-scan is a symlink into the checkout.

        The rules live in a sibling tool (tools/leak-gate), so they are found
        from the scanner's real path; resolved from the link's own directory
        they would be missing and every scan would refuse.
        """
        with tempfile.TemporaryDirectory() as root:
            link = os.path.join(root, "bin", "claude-secret-scan")
            os.makedirs(os.path.dirname(link))
            os.symlink(SCANNER, link)
            target = os.path.join(root, "app.py")
            with open(target, "w") as handle:
                handle.write("KEY = '%s'\n" % FAKE["github-token"])
            result = subprocess.run((sys.executable, link, target),
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("github-token", result.stderr)

    def test_outside_a_repo_blocks_rather_than_passing(self):
        """Fail closed. See the module docstring on claude-secret-scan."""
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run((sys.executable, SCANNER, "--all"),
                                    cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--no-verify", result.stderr)

    def test_binary_and_oversized_files_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as root:
            binary = os.path.join(root, "blob.bin")
            with open(binary, "wb") as handle:
                handle.write(b"\0\1\2" + FAKE["anthropic-key"].encode())
            result = subprocess.run((sys.executable, SCANNER, binary),
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)

    def test_text_that_is_not_utf8_is_still_scanned(self):
        """A latin-1 file with a credential must not pass silently.

        `read()` used to return None on UnicodeDecodeError, which is the same
        thing it returns for "binary" and "too large" — so the caller skipped
        the file and said nothing. That made `--all`, the CI backstop, report
        green over a tracked file it never looked at.

        0x92 is a cp1252 smart quote: no NUL byte, so it is not binary, and
        invalid UTF-8, so it used to be dropped. Every rule is ASCII, so the key
        survives the replacement decode intact.
        """
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "notes.txt")
            with open(target, "wb") as handle:
                handle.write(b"it\x92s a note\n")
                handle.write(('KEY = "%s"\n' % FAKE["anthropic-key"]).encode())
            with open(target, "rb") as handle:
                raw = handle.read()
            self.assertNotIn(b"\0", raw, "fixture must not look binary")
            with self.assertRaises(UnicodeDecodeError,
                                   msg="fixture must actually be undecodable"):
                raw.decode("utf-8")
            result = subprocess.run((sys.executable, SCANNER, target),
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1,
                         "credential in a non-UTF-8 file was skipped silently")
        self.assertIn("anthropic-key", result.stderr)


REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "leak-gate"))
import leak_gate_lib  # noqa: E402


class TestOneTable(unittest.TestCase):
    """The scanner applies the leak gate's credential table, and only that one."""

    def test_rules_equal_the_leak_gate_table_in_order(self):
        ours = [(name, pattern.pattern) for name, pattern in secret_lib.PATTERNS]
        gate = [(name, pattern.pattern.decode("ascii"))
                for name, pattern in leak_gate_lib.SECRET_RULES]
        self.assertEqual(ours, gate)

    def test_no_second_table_in_the_scanner_source(self):
        """A pasted-back copy of a vendor pattern is a second table.

        Equal tables today drift apart the first time only one is edited, so
        no pattern source may appear in the scanner's own files at all.
        """
        for name in ("secret_lib.py", "claude-secret-scan"):
            with open(os.path.join(TOOL_DIR, name), encoding="utf-8") as handle:
                source = handle.read()
            for rule, pattern in leak_gate_lib.SECRET_RULES:
                with self.subTest(file=name, rule=rule):
                    self.assertNotIn(pattern.pattern.decode("ascii"), source)


class TestMissingTableFailsClosed(unittest.TestCase):
    """No rules must never read as a clean scan, in any mode."""

    STUBS = {
        "missing": None,
        "empty": "SECRET_RULES = ()\n",
        "broken": "raise RuntimeError('half-written checkout')\n",
    }

    def _scanner_copy(self, root, stub):
        tool = os.path.join(root, "tools", "secret-scan")
        os.makedirs(tool)
        for name in ("claude-secret-scan", "secret_lib.py"):
            shutil.copy2(os.path.join(TOOL_DIR, name), os.path.join(tool, name))
        if stub is not None:
            gate = os.path.join(root, "tools", "leak-gate")
            os.makedirs(gate)
            with open(os.path.join(gate, "leak_gate_lib.py"), "w") as handle:
                handle.write(stub)
        return os.path.join(tool, "claude-secret-scan")

    def test_every_mode_exits_two(self):
        for label, stub in self.STUBS.items():
            with tempfile.TemporaryDirectory() as root:
                scanner = self._scanner_copy(root, stub)
                repo = os.path.join(root, "repo")
                os.makedirs(repo)
                env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")
                subprocess.run(("git", "init", "-q"), cwd=repo, env=env, check=True)
                target = os.path.join(repo, "app.py")
                with open(target, "w") as handle:
                    handle.write("KEY = '%s'\n" % FAKE["anthropic-key"])
                subprocess.run(("git", "add", "app.py"), cwd=repo, env=env, check=True)
                for mode in ((), ("--all",), (target,)):
                    with self.subTest(table=label, mode=mode or "staged"):
                        result = subprocess.run(
                            (sys.executable, scanner) + mode, cwd=repo, env=env,
                            capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("could not load its rules", result.stderr)
                        self.assertIn("--no-verify", result.stderr)


class TestTreeHasNoCredentialLiteral(unittest.TestCase):
    """The leak gate has no allow marker, so the tree must carry no key shapes."""

    def test_leak_gate_finds_no_credential_in_any_tracked_file(self):
        listed = subprocess.run(("git", "ls-files", "-z"), cwd=REPO_ROOT,
                                capture_output=True, timeout=30)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        paths = [os.fsdecode(p) for p in listed.stdout.split(b"\0") if p]
        self.assertIn("tools/secret-scan/tests/test_secret_scan.py", paths)
        hits = []
        for path in paths:
            full = os.path.join(REPO_ROOT, path)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            with open(full, "rb") as handle:
                data = handle.read()
            for rule, start, _end in leak_gate_lib.secret_hits(data):
                hits.append("%s:%d %s" % (path, data[:start].count(b"\n") + 1, rule))
        self.assertEqual(hits, [])


class TestArgumentsCannotSilentlyPass(unittest.TestCase):
    """A mistyped invocation must not look like a clean scan.

    Every case here exited 0 with no output before: an unrecognised option fell
    through to path mode, where `read()` swallows the OSError from a path that
    is not there and returns None, so nothing was scanned and nothing was said.

    This is not cosmetic. ci/local-checks.sh runs `claude-secret-scan --all` by name as
    the CI backstop behind the pre-commit hook. A rename or a typo in that
    invocation would have left the job green while scanning zero files — the
    same "green run that executed nothing" failure the suites job guards
    against with its `ran == 0` check, inside the tool that is meant to be the
    guard.
    """

    def _run(self, *args, cwd=None):
        return subprocess.run((sys.executable, SCANNER) + args, cwd=cwd,
                              capture_output=True, text=True, timeout=30)

    def test_unrecognised_option_exits_2_and_says_so(self):
        result = self._run("--all-files")
        self.assertEqual(result.returncode, 2,
                         "a typo'd flag reported a clean scan: %r" % result.stdout)
        self.assertIn("unrecognised option", result.stderr)
        self.assertIn("--all-files", result.stderr)

    def test_help_prints_usage_and_exits_0(self):
        result = self._run("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)
        for flag in ("--staged", "--all"):
            self.assertIn(flag, result.stdout)

    def test_named_path_that_does_not_exist_exits_2(self):
        """Distinct from a skipped file. Skipping a binary in a sweep is right;
        silently skipping a file someone named is not."""
        result = self._run("/nonexistent-root/definitely-not-here.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no such file", result.stderr)

    def test_a_sweep_that_scanned_nothing_is_an_error(self):
        """`--all` in a git repo with no tracked files. Green here would read as
        'no credentials are tracked', which is not what was established."""
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(("git", "-C", root, "init", "-q"), check=True,
                           capture_output=True)
            result = self._run("--all", cwd=root)
        self.assertEqual(result.returncode, 2,
                         "an empty sweep passed as clean: %r" % result.stdout)
        self.assertIn("no tracked files", result.stderr)

    def test_the_real_invocation_still_works(self):
        """The guard above must not break the spelling ci/local-checks.sh depends on."""
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(("git", "-C", root, "init", "-q"), check=True,
                           capture_output=True)
            target = os.path.join(root, "clean.py")
            with open(target, "w") as handle:
                handle.write("import os\nkey = os.environ['K']\n")
            subprocess.run(("git", "-C", root, "add", "clean.py"), check=True,
                           capture_output=True)
            result = self._run("--all", cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestStagedScanIsWhatTheHookRuns(unittest.TestCase):
    """The pre-commit path, exercised against a real repo."""

    def _repo(self, root):
        def git(*args):
            subprocess.run(("git", "-C", root) + args, check=True,
                           capture_output=True, text=True, timeout=30)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "Test")
        return git

    def test_staged_secret_is_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            with open(os.path.join(root, "conf.py"), "w") as handle:
                handle.write("KEY = '%s'\n" % FAKE["anthropic-key"])
            git("add", "-A")
            result = subprocess.run((sys.executable, SCANNER),
                                    cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)

    def test_staged_clean_change_passes(self):
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            with open(os.path.join(root, "conf.py"), "w") as handle:
                handle.write("import os\nKEY = os.environ['K']\n")
            git("add", "-A")
            result = subprocess.run((sys.executable, SCANNER),
                                    cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staged_secret_in_non_utf8_file_is_blocked_by_finding_it(self):
        """The hook path must SCAN a latin-1 file, not bail with a codec error.

        `git()` decoded strictly, so `git diff --cached` over this file raised
        UnicodeDecodeError and the tool exited 2: "could not complete: 'utf-8'
        codec can't decode byte 0x92". Safe — nothing reached history — but the
        only way past it is `git commit --no-verify`, which scans the whole
        commit not at all. Exit 1 with the rule named is the difference between
        a gate that works and a gate people learn to route around.
        """
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            with open(os.path.join(root, "notes.txt"), "wb") as handle:
                handle.write(b"it\x92s a note\n")
                handle.write(('KEY = "%s"\n' % FAKE["anthropic-key"]).encode())
            git("add", "-A")
            result = subprocess.run((sys.executable, SCANNER),
                                    cwd=root, capture_output=True, text=True, timeout=30)
        self.assertNotIn("codec can't decode", result.stderr)
        self.assertEqual(result.returncode, 1,
                         "staged non-UTF-8 file not scanned: %s" % result.stderr)
        self.assertIn("anthropic-key", result.stderr)

    def test_unstaged_secret_is_not_blocked(self):
        """Only what is being committed. A secret sitting in the working tree
        is not yet a history problem, and blocking on it would make every
        commit in a repo with a local .env impossible."""
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            with open(os.path.join(root, "ok.py"), "w") as handle:
                handle.write("x = 1\n")
            git("add", "ok.py")
            with open(os.path.join(root, ".env"), "w") as handle:
                handle.write("ANTHROPIC_API_KEY=%s\n" % FAKE["anthropic-key"])
            result = subprocess.run((sys.executable, SCANNER),
                                    cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def _clean_env(self):
        """The environment with every GIT_* variable stripped, so a hook's own
        GIT_DIR / GIT_INDEX_FILE cannot redirect the fixture repo."""
        return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def _staged_scan(self, files, config=()):
        """Stage `files` ({name: bytes}) in a fresh repo; return the scan result."""
        env = self._clean_env()
        with tempfile.TemporaryDirectory() as root:
            def git(*args):
                subprocess.run(("git", "-C", root) + args, check=True, env=env,
                               capture_output=True, text=True, encoding="utf-8", timeout=30)
            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "Test")
            for key, value in config:
                git("config", key, value)
            for name, data in files.items():
                with open(os.path.join(root, name), "wb") as handle:
                    handle.write(data)
            git("add", "-A")
            return subprocess.run((sys.executable, SCANNER), cwd=root, env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  timeout=30)

    def test_staged_plus_plus_line_is_blocked(self):
        result = self._staged_scan(
            {"conf.py": ("++TOKEN=%s\n" % FAKE["anthropic-key"]).encode()})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("anthropic-key", result.stderr)

    def test_staged_carriage_return_does_not_hide_an_added_line(self):
        """Text-mode git output turns a lone \\r into \\n, so the tail of
        the line lost its `+` prefix and was never scanned."""
        result = self._staged_scan(
            {"conf.py": ("x = 1\rKEY = '%s'\n" % FAKE["anthropic-key"]).encode()})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("anthropic-key", result.stderr)

    def test_staged_scan_ignores_mnemonic_prefix(self):
        """diff.mnemonicPrefix writes `i/conf.py`; the parser must not read it."""
        result = self._staged_scan(
            {"conf.py": ("KEY = '%s'\n" % FAKE["anthropic-key"]).encode()},
            config=[("diff.mnemonicPrefix", "true")])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("conf.py:1", result.stderr)
        self.assertNotIn("i/conf.py", result.stderr)

    def test_staged_scan_ignores_noprefix(self):
        result = self._staged_scan(
            {"conf.py": ("KEY = '%s'\n" % FAKE["anthropic-key"]).encode()},
            config=[("diff.noprefix", "true")])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("conf.py:1", result.stderr)


class TestFullSweepSeesEveryTrackedFile(unittest.TestCase):
    """`--all` is the CI backstop, so a file it cannot see must not read as clean.

    Two silent holes: newline-delimited `git ls-files` C-quotes a non-ASCII or
    odd name (`"caf\\303\\251.txt"`), which then fails to open and was skipped;
    and `read()` returned None for a permission error or a directory, the same
    value as "binary", so an unreadable file counted as scanned.
    """

    _clean_env = TestStagedScanIsWhatTheHookRuns._clean_env

    def _sweep(self, files, args=("--all",), after_add=None):
        """Track `files` ({name: bytes}) in a fresh repo, run the scanner there.

        `after_add(root, git)` runs once the files are tracked, before the scan.
        """
        env = self._clean_env()
        root = tempfile.mkdtemp()
        self.addCleanup(self._remove, root)

        def git(*git_args):
            return subprocess.run(("git", "-C", root) + git_args, check=True, env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  timeout=30).stdout
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "Test")
        for name, data in files.items():
            with open(os.path.join(root, name), "wb") as handle:
                handle.write(data)
        git("add", "-A")
        if after_add:
            after_add(root, git)
        return self._scan(root, env, args)

    def _scan(self, root, env, args=("--all",)):
        return subprocess.run((sys.executable, SCANNER) + tuple(args), cwd=root, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    def _remove(self, root):
        # A chmod-0 fixture cannot be deleted until it is readable again.
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not os.path.islink(path):
                try:
                    os.chmod(path, 0o644)
                except OSError:
                    pass
        shutil.rmtree(root, ignore_errors=True)

    def _key_file(self):
        return ("KEY = '%s'\n" % FAKE["anthropic-key"]).encode()

    def _lock(self, root, name):
        path = os.path.join(root, name)
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)

    def test_non_ascii_filename_is_scanned(self):
        result = self._sweep({"café.txt": self._key_file()})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("anthropic-key", result.stderr)

    @unittest.skipIf(os.name == "nt", "tab and newline are not legal in Windows names")
    def test_tab_and_newline_in_filename_are_scanned(self):
        result = self._sweep({"tab\tname.txt": self._key_file(),
                              "new\nline.txt": self._key_file()})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr.count("anthropic-key"), 2, result.stderr)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "chmod 0 does not block reads on Windows or as root")
    def test_unreadable_tracked_file_is_incomplete_not_clean(self):
        result = self._sweep({"locked.txt": b"x = 1\n"},
                             after_add=lambda root, git: self._lock(root, "locked.txt"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("locked.txt", result.stderr)
        self.assertIn("--no-verify", result.stderr)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "chmod 0 does not block reads on Windows or as root")
    def test_unreadable_file_does_not_hide_findings_elsewhere(self):
        result = self._sweep({"locked.txt": b"x = 1\n", "conf.py": self._key_file()},
                             after_add=lambda root, git: self._lock(root, "locked.txt"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("anthropic-key", result.stderr)
        self.assertIn("locked.txt", result.stderr)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_tracked_symlink_to_directory_is_skipped(self):
        def link(root, git):
            os.mkdir(os.path.join(root, "d"))
            os.symlink("d", os.path.join(root, "link"))
            git("add", "link")
        result = self._sweep({"clean.py": b"x = 1\n"}, after_add=link)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gitlink_entry_is_skipped(self):
        def gitlink(root, git):
            git("commit", "-q", "--no-verify", "-m", "init")
            head = git("rev-parse", "HEAD").strip()
            os.mkdir(os.path.join(root, "sub"))
            git("update-index", "--add", "--cacheinfo", "160000,%s,sub" % head)
        result = self._sweep({"clean.py": b"x = 1\n"}, after_add=gitlink)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_file_deleted_in_working_tree_is_skipped(self):
        outcome = {}

        def delete(root, git):
            git("commit", "-q", "--no-verify", "-m", "init")
            os.remove(os.path.join(root, "gone.txt"))
            env = self._clean_env()
            outcome["before"] = self._scan(root, env)
            # Positive control: the sweep still ran and still finds things.
            with open(os.path.join(root, "conf.py"), "wb") as handle:
                handle.write(self._key_file())
        result = self._sweep({"gone.txt": b"x = 1\n", "conf.py": b"y = 2\n"},
                             after_add=delete)
        self.assertEqual(outcome["before"].returncode, 0, outcome["before"].stderr)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("anthropic-key", result.stderr)

    def test_named_directory_is_incomplete(self):
        def named(root, git):
            os.mkdir(os.path.join(root, "adir"))
        result = self._sweep({"clean.py": b"x = 1\n"}, args=("adir",), after_add=named)
        self.assertEqual(result.returncode, 2, result.stderr)


class TestPreCommitHookChaining(unittest.TestCase):
    """core.hooksPath REPLACES per-repo hooks. Our hook must chain to them.

    This exists because the first implementation did not. It used
    `git rev-parse --git-path hooks`, which resolves THROUGH core.hooksPath and
    therefore returned our own directory — so the repo's real pre-commit hook
    was never found, and installing secret scanning would have silently
    disabled every existing pre-commit hook on the machine. It reported success
    the whole time. Nothing but running it caught this.
    """

    HOOK = os.path.join(TOOL_DIR, "git-pre-commit")

    def _repo(self, root, own_hook_body=None):
        def git(*args, **kwargs):
            return subprocess.run(("git", "-C", root) + args, capture_output=True,
                                  text=True, timeout=30, **kwargs)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "Test")
        hooks = os.path.join(root, ".githooks")
        os.makedirs(hooks, exist_ok=True)
        os.symlink(self.HOOK, os.path.join(hooks, "pre-commit"))
        git("config", "core.hooksPath", ".githooks")
        if own_hook_body is not None:
            own_dir = os.path.join(root, ".git", "hooks")
            os.makedirs(own_dir, exist_ok=True)
            own = os.path.join(own_dir, "pre-commit")
            with open(own, "w") as handle:
                handle.write(own_hook_body)
            os.chmod(own, 0o755)
        return git

    def _commit(self, git, root, name, body):
        with open(os.path.join(root, name), "w") as handle:
            handle.write(body)
        git("add", "-A")
        return git("commit", "-m", "msg")

    def _count(self, git):
        result = git("rev-list", "--count", "HEAD")
        return int(result.stdout.strip()) if result.returncode == 0 else 0

    def test_repos_own_hook_still_runs(self):
        with tempfile.TemporaryDirectory() as root:
            proof = os.path.join(root, "proof.txt")
            git = self._repo(root, "#!/bin/bash\necho RAN > %s\nexit 0\n" % proof)
            self._commit(git, root, "a.py", "a = 1\n")
            self.assertTrue(os.path.exists(proof),
                            "the repo's own pre-commit hook was silently skipped")
            self.assertEqual(self._count(git), 1)

    def test_repos_own_hook_can_still_block(self):
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root, "#!/bin/bash\nexit 1\n")
            self._commit(git, root, "a.py", "a = 1\n")
            self.assertEqual(self._count(git), 0, "a failing repo hook did not block")

    def test_oversized_claude_md_blocks_the_commit(self):
        """The size cap fires THROUGH this hook, not just in its own unit tests.

        Without this, the chaining step added to git-pre-commit is an untested
        path: every test in test_context_lint_lib.py would still pass with the
        linter wired to nothing at all.

        SIZED OFF DEFAULT_MAX_LINES, never a literal. This was "x\n" * 500
        against a 400 default; when the cap was standardized to 800 on
        2026-09-15 the fixture stopped being over cap and this test went red —
        correctly, but for a fixture reason rather than a wiring one. It is the
        only test that proves the linter is chained into the hook at all, so it
        must not depend on a number that lives in another tool.
        """
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            self._commit(git, root, "CLAUDE.md",
                         "x\n" * (context_lint_lib.DEFAULT_MAX_LINES + 100))
            self.assertEqual(self._count(git), 0,
                             "an over-cap CLAUDE.md was committed")

    def test_normal_sized_claude_md_still_commits(self):
        """Negative control. A block that fired unconditionally would satisfy
        the test above while breaking every commit that touches a CLAUDE.md."""
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            self._commit(git, root, "CLAUDE.md", "x\n" * 10)
            self.assertEqual(self._count(git), 1,
                             "a compliant CLAUDE.md was wrongly blocked")

    def test_size_cap_does_not_preempt_the_secret_scan(self):
        """Ordering: a commit carrying BOTH must report the credential.

        The scanner runs first on purpose — a leaked key is unbounded cost and
        its message must not be buried under a line-count complaint.

        THE CLAUDE.md MUST GENUINELY BE OVER LIMIT or this test stops being about
        ordering at all. It was "x\n" * 500 against a 400 default; once the cap
        was standardized to 800 on 2026-09-15 that file was comfortably under,
        so the commit carried a credential and a PERFECTLY FINE CLAUDE.md —
        and `assertNotIn("BLOCKED: a CLAUDE.md")` passed for the trivial reason
        that there was nothing for the size cap to say. Green, and testing
        nothing. Sized off DEFAULT_MAX_LINES so it tracks the cap.
        """
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            with open(os.path.join(root, "CLAUDE.md"), "w") as handle:
                handle.write("x\n" * (context_lint_lib.DEFAULT_MAX_LINES + 100))
            with open(os.path.join(root, "conf.py"), "w") as handle:
                handle.write("K = '%s'\n" % FAKE["anthropic-key"])
            git("add", "-A")
            result = git("commit", "-m", "msg")
            self.assertEqual(self._count(git), 0)
            combined = (result.stdout or "") + (result.stderr or "")
            self.assertNotIn("BLOCKED: a CLAUDE.md", combined,
                             "the size cap preempted the secret scanner")

    def test_secret_blocks_the_commit(self):
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            self._commit(git, root, "conf.py", "K = '%s'\n" % FAKE["anthropic-key"])
            self.assertEqual(self._count(git), 0, "a staged credential was committed")

    def test_clean_commit_passes(self):
        with tempfile.TemporaryDirectory() as root:
            git = self._repo(root)
            self._commit(git, root, "conf.py", 'K = "op://Vault/Item/credential"\n')
            self.assertEqual(self._count(git), 1)


class TestHookForwarding(unittest.TestCase):
    """git-hook-forward runs the repo's own hook of the same name.

    core.hooksPath replaces per-repo hooks, so every client-side hook name other
    than pre-commit needs a link to the forwarder or the repo's own hook goes
    silent. Each test links ONE hook name to the forwarder in an absolute
    core.hooksPath (a relative one resolves against a linked worktree's root,
    where the directory does not exist).
    """

    FORWARDER = os.path.join(TOOL_DIR, "git-hook-forward")

    def _env(self):
        # An inherited GIT_DIR / GIT_* would aim the fixture's git at a real repo.
        return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def _git(self, root, *args, stdin=None):
        return subprocess.run(("git", "-C", root) + args, capture_output=True,
                              text=True, timeout=30, env=self._env(), input=stdin)

    def _repo(self, root, linked, own_hooks=None, commit=False):
        """Init a repo whose core.hooksPath holds links named in `linked`.

        own_hooks maps hook name -> script body installed at .git/hooks/<name>.
        """
        self._git(root, "init", "-q", "-b", "main")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "Test")
        hooks = os.path.join(root, ".githooks")
        os.makedirs(hooks, exist_ok=True)
        for name in linked:
            os.symlink(self.FORWARDER, os.path.join(hooks, name))
        self._git(root, "config", "core.hooksPath", hooks)
        own_dir = os.path.join(root, ".git", "hooks")
        os.makedirs(own_dir, exist_ok=True)
        for name, body in (own_hooks or {}).items():
            own = os.path.join(own_dir, name)
            with open(own, "w") as handle:
                handle.write(body)
            os.chmod(own, 0o755)
        if commit:
            self._commit(root, "seed.txt", "seed\n")

    def _commit(self, root, name, body, message="msg"):
        with open(os.path.join(root, name), "w") as handle:
            handle.write(body)
        self._git(root, "add", "-A")
        return self._git(root, "commit", "-q", "-m", message)

    def _count(self, root):
        result = self._git(root, "rev-list", "--count", "HEAD")
        return int(result.stdout.strip()) if result.returncode == 0 else 0

    def _bare_remote(self, parent):
        bare = os.path.join(parent, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare],
                       capture_output=True, timeout=30, env=self._env(), check=True)
        return bare

    def test_commit_msg_receives_the_message_file_and_runs(self):
        """AC3.1: $1 is the message file; the own hook sees the message."""
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "repo")
            os.makedirs(root)
            proof = os.path.join(parent, "proof.txt")
            self._repo(root, ["commit-msg"],
                       {"commit-msg": "#!/bin/bash\ncat \"$1\" > %s\nexit 0\n" % proof})
            result = self._commit(root, "a.py", "a = 1\n", message="hello-forwarder")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.exists(proof), "the repo's own commit-msg hook was skipped")
            with open(proof) as handle:
                self.assertIn("hello-forwarder", handle.read())

    def test_commit_msg_own_hook_can_block(self):
        """AC3.1: an own hook exiting 1 blocks the commit."""
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "repo")
            os.makedirs(root)
            self._repo(root, ["commit-msg"], {"commit-msg": "#!/bin/bash\nexit 1\n"})
            result = self._commit(root, "a.py", "a = 1\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self._count(root), 0, "a failing own commit-msg did not block")

    def test_pre_push_receives_remote_args_and_ref_lines(self):
        """AC3.2: $1 $2 are remote name and URL; ref lines arrive on stdin."""
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "repo")
            os.makedirs(root)
            bare = self._bare_remote(parent)
            args_proof = os.path.join(parent, "args.txt")
            stdin_proof = os.path.join(parent, "stdin.txt")
            body = "#!/bin/bash\necho \"$1 $2\" > %s\ncat > %s\nexit 0\n" % (args_proof, stdin_proof)
            self._repo(root, ["pre-push"], {"pre-push": body}, commit=True)
            self._git(root, "remote", "add", "origin", bare)
            result = self._git(root, "push", "-q", "origin", "main")
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(args_proof) as handle:
                self.assertEqual(handle.read().split(), ["origin", bare])
            with open(stdin_proof) as handle:
                self.assertIn("refs/heads/main", handle.read())

    def test_pre_push_own_hook_can_block(self):
        """AC3.2: an own pre-push exiting 1 blocks the push."""
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "repo")
            os.makedirs(root)
            bare = self._bare_remote(parent)
            self._repo(root, ["pre-push"], {"pre-push": "#!/bin/bash\nexit 1\n"}, commit=True)
            self._git(root, "remote", "add", "origin", bare)
            result = self._git(root, "push", "-q", "origin", "main")
            self.assertNotEqual(result.returncode, 0)
            remote_ref = self._git(bare, "rev-parse", "--verify", "-q", "refs/heads/main")
            self.assertNotEqual(remote_ref.returncode, 0, "the push landed despite a failing hook")

    def test_post_checkout_from_a_linked_worktree_runs_the_main_checkouts_hook(self):
        """AC3.3: hooks live in the main checkout and are shared by worktrees."""
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "repo")
            os.makedirs(root)
            proof = os.path.join(parent, "proof.txt")
            self._repo(root, ["post-checkout"],
                       {"post-checkout": "#!/bin/bash\necho RAN > %s\nexit 0\n" % proof},
                       commit=True)
            worktree = os.path.join(parent, "wt")
            added = self._git(root, "worktree", "add", "-q", "-b", "wt-branch", worktree)
            self.assertEqual(added.returncode, 0, added.stderr)
            # `worktree add` fires post-checkout itself; clear that so the
            # assertion below is about the checkout run FROM the worktree.
            if os.path.exists(proof):
                os.remove(proof)
            result = self._git(worktree, "checkout", "-q", "-b", "wt-branch-2")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.exists(proof),
                            "the main checkout's own post-checkout did not run from a worktree")

    def test_no_own_hook_never_blocks(self):
        """AC3.4: with no own hook, commit, push and a ref update all succeed."""
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "repo")
            os.makedirs(root)
            bare = self._bare_remote(parent)
            self._repo(root, ["commit-msg", "pre-push", "reference-transaction"])
            committed = self._commit(root, "a.py", "a = 1\n")
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertEqual(self._count(root), 1)
            self._git(root, "remote", "add", "origin", bare)
            pushed = self._git(root, "push", "-q", "origin", "main")
            self.assertEqual(pushed.returncode, 0, pushed.stderr)
            branched = self._git(root, "branch", "x")
            self.assertEqual(branched.returncode, 0, branched.stderr)

    def test_own_hook_that_is_the_forwarder_does_not_recurse(self):
        """AC3.5: a per-repo link to the forwarder must not loop forever."""
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "repo")
            os.makedirs(root)
            self._repo(root, ["commit-msg"])
            os.symlink(self.FORWARDER, os.path.join(root, ".git", "hooks", "commit-msg"))
            with open(os.path.join(root, "a.py"), "w") as handle:
                handle.write("a = 1\n")
            self._git(root, "add", "-A")
            # 30 s is a catastrophe bound for an infinite exec loop, not a budget.
            result = subprocess.run(["git", "-C", root, "commit", "-q", "-m", "msg"],
                                    capture_output=True, text=True, timeout=30,
                                    env=self._env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._count(root), 1)

    def test_own_hook_path_that_is_a_directory_is_ignored_like_git_does(self):
        """Native git ignores a directory at .git/hooks/<name>; forwarding must
        not turn it into a blocked commit (Code Review F1)."""
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "repo")
            os.makedirs(root)
            self._repo(root, ["commit-msg"])
            os.makedirs(os.path.join(root, ".git", "hooks", "commit-msg"))
            with open(os.path.join(root, "a.py"), "w") as handle:
                handle.write("a = 1\n")
            self._git(root, "add", "-A")
            result = subprocess.run(["git", "-C", root, "commit", "-q", "-m", "msg"],
                                    capture_output=True, text=True, timeout=30,
                                    env=self._env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._count(root), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
