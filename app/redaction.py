from __future__ import annotations

import re

KNOWN_PEOPLE = [
    "A. Rao",
    "P. Kumar",
    "S. Mehta",
    "N. Singh",
    "T. Lead",
]

SECRET_PATTERNS = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\b(?:\+91[-\s]?)?[6-9]\d{9}\b"), "[PHONE]"),
]

PHONE_OR_EMAIL = re.compile(
    r"(\[EMAIL\]|\[PHONE\]|\[REDACTED_[A-Z_]+\])"
)


def redact(text: str, people: list[str] | None = None) -> tuple[str, dict]:
    """Return text with secrets and identities replaced, plus a report."""
    people = people or KNOWN_PEOPLE
    report = {"secrets": 0, "identities": 0}
    out = text
    for pattern, replacement in SECRET_PATTERNS:
        out, n = pattern.subn(replacement, out)
        report["secrets"] += n
    for i, name in enumerate(people, start=1):
        # use a word-boundary-ish replace that tolerates initials and spaces
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        out, n = pattern.subn(f"PERSON_{i}", out)
        report["identities"] += n
    return out, report


def redaction_preview(text: str, limit: int = 900) -> dict:
    redacted, report = redact(text)
    if len(redacted) > limit:
        redacted = redacted[:limit] + "\n... [preview truncated]"
    return {
        "text": redacted,
        "secrets": report["secrets"],
        "identities": report["identities"],
    }
