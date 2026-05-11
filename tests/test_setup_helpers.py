"""Tests for scripts/setup_helpers.py (lane PB onboarding helpers).

Covers:

  1. detect_dev_context inside a real git repo -> all flags positive
  2. detect_dev_context outside any git repo -> in_git_repo False, root None
  3. detect_dev_context when git is unavailable -> git_available False
  4. archetype_hint_from_email_domain(None) -> default ("job", ...)
  5. archetype_hint_from_email_domain("") -> default ("job", ...)
  6. gmail.com -> ("personal", ...)
  7. *.edu -> ("exploring", ...)
  8. unknown corporate domain -> default ("job", ...)
  9. Determinism: same input twice -> same output
 10. Reason string is non-empty for every branch
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Make scripts/ importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import setup_helpers  # noqa: E402


# ---------------------------------------------------------------------------
# detect_dev_context
# ---------------------------------------------------------------------------


class DetectDevContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_cwd = os.getcwd()
        self.addCleanup(lambda: os.chdir(self._orig_cwd))

    def test_inside_git_repo(self):
        """cwd inside a real git repo -> all positive, project_root set."""
        repo = Path(self._tmp.name) / "myrepo"
        repo.mkdir()
        # init a real (empty) repo so rev-parse --show-toplevel works.
        subprocess.run(
            ["git", "init", "-q", str(repo)],
            check=True,
            capture_output=True,
        )
        os.chdir(str(repo))
        ctx = setup_helpers.detect_dev_context()
        self.assertTrue(ctx["git_available"])
        self.assertTrue(ctx["in_git_repo"])
        self.assertIsNotNone(ctx["project_root"])
        # Compare resolved paths (macOS /var vs /private/var symlink).
        self.assertEqual(
            Path(str(ctx["project_root"])).resolve(),
            repo.resolve(),
        )

    def test_outside_git_repo(self):
        """cwd in a plain tmp dir -> in_git_repo False, project_root None."""
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        os.chdir(str(plain))
        ctx = setup_helpers.detect_dev_context()
        # git itself is still available on the runner; the cwd isn't a repo.
        self.assertFalse(ctx["in_git_repo"])
        self.assertIsNone(ctx["project_root"])

    def test_git_unavailable(self):
        """which() returns None -> git_available False, downstream untouched."""
        with mock.patch.object(setup_helpers.shutil, "which", return_value=None):
            ctx = setup_helpers.detect_dev_context()
        self.assertFalse(ctx["git_available"])
        self.assertFalse(ctx["in_git_repo"])
        self.assertIsNone(ctx["project_root"])

    def test_never_raises_on_subprocess_failure(self):
        """OSError from subprocess.run -> graceful fallback, no exception."""
        with mock.patch.object(
            setup_helpers.subprocess,
            "run",
            side_effect=OSError("boom"),
        ):
            ctx = setup_helpers.detect_dev_context()
        self.assertTrue(ctx["git_available"])  # which() still hits
        self.assertFalse(ctx["in_git_repo"])
        self.assertIsNone(ctx["project_root"])


# ---------------------------------------------------------------------------
# archetype_hint_from_email_domain
# ---------------------------------------------------------------------------


class ArchetypeHintTests(unittest.TestCase):
    def test_none_input_defaults_to_job(self):
        archetype, reason = setup_helpers.archetype_hint_from_email_domain(None)
        self.assertEqual(archetype, "job")
        self.assertTrue(reason)

    def test_empty_input_defaults_to_job(self):
        archetype, reason = setup_helpers.archetype_hint_from_email_domain("")
        self.assertEqual(archetype, "job")
        self.assertTrue(reason)

    def test_whitespace_input_defaults_to_job(self):
        archetype, reason = setup_helpers.archetype_hint_from_email_domain("   ")
        self.assertEqual(archetype, "job")
        self.assertTrue(reason)

    def test_gmail_is_personal(self):
        archetype, reason = setup_helpers.archetype_hint_from_email_domain(
            "foo@gmail.com"
        )
        self.assertEqual(archetype, "personal")
        self.assertTrue(reason)

    def test_outlook_is_personal(self):
        archetype, _ = setup_helpers.archetype_hint_from_email_domain(
            "someone@outlook.com"
        )
        self.assertEqual(archetype, "personal")

    def test_proton_is_personal(self):
        archetype, _ = setup_helpers.archetype_hint_from_email_domain(
            "x@proton.me"
        )
        self.assertEqual(archetype, "personal")

    def test_edu_tld_is_exploring(self):
        archetype, reason = setup_helpers.archetype_hint_from_email_domain(
            "foo@stanford.edu"
        )
        self.assertEqual(archetype, "exploring")
        self.assertTrue(reason)

    def test_ac_uk_suffix_is_exploring(self):
        archetype, _ = setup_helpers.archetype_hint_from_email_domain(
            "researcher@cs.ox.ac.uk"
        )
        self.assertEqual(archetype, "exploring")

    def test_unknown_corporate_defaults_to_job(self):
        archetype, reason = setup_helpers.archetype_hint_from_email_domain(
            "foo@bigcorp.com"
        )
        self.assertEqual(archetype, "job")
        self.assertTrue(reason)

    def test_uppercase_input_normalizes(self):
        archetype, _ = setup_helpers.archetype_hint_from_email_domain(
            "Foo@Gmail.COM"
        )
        self.assertEqual(archetype, "personal")

    def test_bare_domain_accepted(self):
        archetype, _ = setup_helpers.archetype_hint_from_email_domain("gmail.com")
        self.assertEqual(archetype, "personal")

    def test_determinism(self):
        inputs = [
            None,
            "",
            "foo@gmail.com",
            "foo@stanford.edu",
            "foo@bigcorp.com",
        ]
        for value in inputs:
            first = setup_helpers.archetype_hint_from_email_domain(value)
            second = setup_helpers.archetype_hint_from_email_domain(value)
            self.assertEqual(first, second, f"non-deterministic for {value!r}")

    def test_reason_non_empty_for_every_branch(self):
        for value in [
            None,
            "",
            "foo@gmail.com",
            "foo@stanford.edu",
            "foo@bigcorp.com",
            "foo@cs.ox.ac.uk",
            "foo@proton.me",
        ]:
            _, reason = setup_helpers.archetype_hint_from_email_domain(value)
            self.assertIsInstance(reason, str)
            self.assertGreater(len(reason.strip()), 0, f"empty reason for {value!r}")


# ---------------------------------------------------------------------------
# domain_match (helper exposed for testing)
# ---------------------------------------------------------------------------


class DomainMatchTests(unittest.TestCase):
    def test_unknown_domain_returns_none(self):
        self.assertIsNone(setup_helpers.domain_match("foo@bigcorp.com"))

    def test_known_full_domain(self):
        self.assertEqual(
            setup_helpers.domain_match("foo@gmail.com"),
            ("personal", "personal email provider"),
        )

    def test_known_tld(self):
        result = setup_helpers.domain_match("foo@mit.edu")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "exploring")


if __name__ == "__main__":
    unittest.main()
