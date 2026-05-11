"""Lane PB onboarding helpers.

Small library invoked by the ``/ai-quickstart:setup`` skill. Two jobs:

* ``detect_dev_context()``: best-effort sniff of "are we in a dev
  environment that warrants offering compathy + the lane-p PostToolUse
  hook?" The setup skill skips those steps if the answer is no.
* ``archetype_hint_from_email_domain(email)``: read ``git config
  user.email`` and produce a default archetype suggestion to seed the
  interview. This is a HINT only; the user always confirms or overrides
  in the conversational flow.

Stdlib only. No CLI entry point: import or shell out via ``python3 -c``.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Dev-context detection
# ---------------------------------------------------------------------------


def detect_dev_context() -> Dict[str, object]:
    """Return whether we look like a project / dev environment.

    Shape::

        {
            "git_available": bool,
            "in_git_repo": bool,
            "project_root": str | None,
        }

    ``project_root`` is set to ``git rev-parse --show-toplevel`` when both
    git is available and the cwd lives inside a working tree. Best-effort:
    NEVER raises. Any subprocess / FileNotFoundError / permission error is
    treated as "not a dev context".
    """
    result: Dict[str, object] = {
        "git_available": False,
        "in_git_repo": False,
        "project_root": None,
    }

    git_path = shutil.which("git")
    if not git_path:
        return result
    result["git_available"] = True

    try:
        proc = subprocess.run(
            [git_path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return result

    if proc.returncode != 0:
        return result

    root = (proc.stdout or "").strip()
    if not root:
        return result

    result["in_git_repo"] = True
    result["project_root"] = root
    return result


# ---------------------------------------------------------------------------
# Email-domain -> archetype hint
# ---------------------------------------------------------------------------

# Curated lookup. Keys are either full domains (``"gmail.com"``) or bare
# TLDs (``"edu"``) matched against the suffix. Order matters: full-domain
# matches win over TLD matches (see ``domain_match``).
#
# Archetypes are the same three accepted by init.py:
#   - "exploring": likely student / hobbyist / new to the space
#   - "personal":  consumer-grade personal email, casual side projects
#   - "job":       work-context email, default for unknown corporate
EMAIL_DOMAIN_HINTS: Dict[str, Tuple[str, str]] = {
    # Academic TLDs and common patterns.
    "edu": ("exploring", "academic email"),
    "ac.uk": ("exploring", "academic email"),
    "edu.au": ("exploring", "academic email"),
    # Personal email providers.
    "gmail.com": ("personal", "personal email provider"),
    "googlemail.com": ("personal", "personal email provider"),
    "outlook.com": ("personal", "personal email provider"),
    "hotmail.com": ("personal", "personal email provider"),
    "live.com": ("personal", "personal email provider"),
    "yahoo.com": ("personal", "personal email provider"),
    "icloud.com": ("personal", "personal email provider"),
    "me.com": ("personal", "personal email provider"),
    "mac.com": ("personal", "personal email provider"),
    "proton.me": ("personal", "personal email provider"),
    "protonmail.com": ("personal", "personal email provider"),
    "pm.me": ("personal", "personal email provider"),
    "fastmail.com": ("personal", "personal email provider"),
    "fastmail.fm": ("personal", "personal email provider"),
    "duck.com": ("personal", "personal email provider"),
}

_DEFAULT_HINT: Tuple[str, str] = ("job", "default")


def _extract_domain(email_or_domain: Optional[str]) -> Optional[str]:
    """Pull the lowercase domain out of an email or bare-domain string."""
    if not email_or_domain:
        return None
    s = email_or_domain.strip().lower()
    if not s:
        return None
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    s = s.strip().strip(".")
    return s or None


def domain_match(
    email_or_domain: Optional[str],
) -> Optional[Tuple[str, str]]:
    """Look up a curated hint for ``email_or_domain``.

    Returns the matching ``(archetype, reason)`` pair or ``None`` if the
    domain is unknown (caller falls back to the default). Match order:

      1. Full-domain exact match (``foo@gmail.com`` -> ``gmail.com``).
      2. Suffix match on multi-part TLD keys (``foo@cs.ox.ac.uk`` -> ``ac.uk``).
      3. Final-label TLD match (``foo@stanford.edu`` -> ``edu``).
    """
    domain = _extract_domain(email_or_domain)
    if not domain:
        return None

    if domain in EMAIL_DOMAIN_HINTS:
        return EMAIL_DOMAIN_HINTS[domain]

    # Suffix match: walk the multi-part keys (those containing a dot)
    # and accept the longest suffix match. This lets ``ac.uk`` win over
    # ``uk`` if both were ever in the table.
    multi = [k for k in EMAIL_DOMAIN_HINTS if "." in k]
    multi.sort(key=len, reverse=True)
    for key in multi:
        if domain == key or domain.endswith("." + key):
            return EMAIL_DOMAIN_HINTS[key]

    # Final-label TLD match (e.g. "edu").
    tld = domain.rsplit(".", 1)[-1]
    if tld in EMAIL_DOMAIN_HINTS:
        return EMAIL_DOMAIN_HINTS[tld]

    return None


def archetype_hint_from_email_domain(
    email: Optional[str],
) -> Tuple[str, str]:
    """Suggest a default archetype + a short human-readable reason.

    Pure function: same input always returns the same output. Never
    raises. ``None`` / empty / whitespace-only input falls through to
    the default ``("job", "default")`` pair.
    """
    hit = domain_match(email)
    if hit is not None:
        return hit
    return _DEFAULT_HINT


__all__ = [
    "EMAIL_DOMAIN_HINTS",
    "archetype_hint_from_email_domain",
    "detect_dev_context",
    "domain_match",
]
