"""Personal-identifier detection and input rejection.

The chatbot never needs personal data, so obvious identifiers are detected
locally and the question is rejected *before* anything is sent to an LLM.

**This is heuristic and cannot be complete.** It is not anonymisation: it does
not and cannot guarantee that every personal detail is found. Names, informal
addresses and case narratives cannot be detected reliably by pattern matching,
which is exactly why the interface also warns users not to submit personal
information.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Each pattern is deliberately conservative: it should catch clear-cut cases
# without flagging ordinary legal terminology.
PATTERNS: dict[str, re.Pattern[str]] = {
    # someone@example.com
    "email": re.compile(r"\b[\w.%+-]+@[\w-]+\.[A-Za-z]{2,}\b"),
    # +263 77 123 4567 / 0771234567 / 077 123 4567
    "phone_number": re.compile(
        r"(?<!\d)(?:\+?263|0)\s?7[1-8]\s?(?:\d[\s-]?){6}\d(?!\d)"
    ),
    # Zimbabwe-style national ID: 63-1234567-A-42
    "national_id": re.compile(r"\b\d{2}[-\s]?\d{6,7}[-\s]?[A-Za-z][-\s]?\d{2}\b"),
    # Any long unbroken digit run (passport, account, file reference)
    "long_number": re.compile(r"(?<!\d)\d{9,}(?!\d)"),
    # Court case references such as HC 1234/24 or SC 12/2024
    "case_number": re.compile(
        r"\b(?:HC|SC|CC|MC|CHC|HRE|BYO|CIV|CRB|APP)\s?\d{1,5}\s?/\s?\d{2,4}\b"
    ),
    # "stand 1234", "plot 56", "house number 12"
    "physical_address": re.compile(
        r"\b(?:stand|plot|house|flat|farm)\s*(?:no\.?|number|#)?\s*\d{1,5}\b",
        re.IGNORECASE,
    ),
}

CATEGORY_LABELS = {
    "email": "an email address",
    "phone_number": "a phone number",
    "national_id": "what looks like a national identity number",
    "long_number": "a long number that may be an identifier or file reference",
    "case_number": "what looks like a case number",
    "physical_address": "what looks like a physical address",
}


@dataclass(frozen=True)
class PrivacyScan:
    """Outcome of scanning a message for obvious personal identifiers."""

    detected: bool = False
    categories: tuple[str, ...] = field(default_factory=tuple)
    # Always True: the detector is a heuristic and cannot be exhaustive.
    heuristic_only: bool = True
    # True once the scan has actually run, so callers never read a default as
    # "checked and clean".
    evaluated: bool = True

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(CATEGORY_LABELS.get(name, name) for name in self.categories)

    def message(self) -> str:
        """A user-facing explanation of why the input was not processed."""
        if not self.detected:
            return ""
        listed = ", ".join(self.labels)
        return (
            "I have not processed that question because it appears to contain "
            f"{listed}. Please remove any personal or case-specific details and "
            "ask about the general term or procedure instead. I only need the "
            "legal term you want explained."
        )


def scan(text: str) -> PrivacyScan:
    """Return which obvious personal-identifier categories appear in ``text``."""
    if not text:
        return PrivacyScan()

    found = [category for category, pattern in PATTERNS.items() if pattern.search(text)]
    return PrivacyScan(detected=bool(found), categories=tuple(found))
