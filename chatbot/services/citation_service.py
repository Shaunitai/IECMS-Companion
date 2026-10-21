"""Citation validation.

The model must never invent citations. Retrieved passages are presented to the
model with stable labels (``[S1]``, ``[S2]`` ...). After generation, every
marker in the answer is checked against the labels that were actually supplied:

- markers that match a supplied passage become displayed citations;
- markers that do not match are **stripped** from the answer and recorded, so a
  fabricated reference can never reach a user.

Validation is deterministic and does not depend on the model behaving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .retrieval_service import RetrievedPassage

_MARKER = re.compile(r"\[S(\d{1,3})\]")

# A citation is only usable if the underlying source is allowed to ground an
# authoritative answer.
SYNTHETIC_STATUS = "synthetic"
AUTHORITATIVE_STATUSES = frozenset({"verified"})


class CitationValidationError(ValueError):
    """Raised when a proposed citation cannot be validated."""


@dataclass(frozen=True)
class Citation:
    """A validated, displayable source reference."""

    label: str
    passage_id: str
    document_title: str
    issuing_authority: str
    document_type: str
    tier_label: str
    source_url: str
    verification_status: str
    disclosure: str
    page_number: int | None = None
    section_heading: str = ""
    # A same-origin URL that serves the stored document, present when the
    # source is a local file. The browser appends a #page= anchor to it.
    local_url: str = ""

    @property
    def is_synthetic(self) -> bool:
        return self.verification_status == SYNTHETIC_STATUS

    @property
    def is_authoritative(self) -> bool:
        return self.verification_status in AUTHORITATIVE_STATUSES

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "passage_id": self.passage_id,
            "document_title": self.document_title,
            "issuing_authority": self.issuing_authority,
            "document_type": self.document_type,
            "tier_label": self.tier_label,
            "source_url": self.source_url,
            "verification_status": self.verification_status,
            "disclosure": self.disclosure,
            "page_number": self.page_number,
            "section_heading": self.section_heading,
            "local_url": self.local_url,
            "synthetic": self.is_synthetic,
        }


@dataclass(frozen=True)
class CitationValidation:
    """Outcome of validating the markers used in a generated answer."""

    text: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    invalid_labels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_citations(self) -> bool:
        return bool(self.citations)

    @property
    def all_synthetic(self) -> bool:
        return bool(self.citations) and all(c.is_synthetic for c in self.citations)

    def to_dicts(self) -> list[dict]:
        return [citation.to_dict() for citation in self.citations]


def _build_citation(label: str, passage: RetrievedPassage) -> Citation:
    return Citation(
        label=label,
        passage_id=passage.passage_id,
        document_title=passage.document_title,
        issuing_authority=passage.issuing_authority,
        document_type=passage.document_type,
        tier_label=passage.tier_label,
        source_url=passage.source_url,
        verification_status=passage.verification_status,
        disclosure=passage.disclosure,
        page_number=passage.page_number,
        section_heading=passage.section_heading,
        local_url=(
            f"/sources/{passage.source_id}/" if passage.local_path else ""
        ),
    )


def validate_citations(
    answer_text: str,
    labelled_passages: list[tuple[str, RetrievedPassage]],
) -> CitationValidation:
    """Validate ``answer_text`` against the passages that were actually supplied.

    Any marker that does not correspond to a supplied passage is removed from the
    returned text and reported in ``invalid_labels``.
    """
    if not answer_text:
        raise CitationValidationError("There is no answer text to validate.")

    supplied = {label: passage for label, passage in labelled_passages}
    cited_labels: list[str] = []
    invalid_labels: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        # ``match.group(0)`` is "[S1]"; the labels supplied to the model are
        # "S1", so build the label from the captured digits.
        label = f"S{match.group(1)}"
        if label in supplied:
            if label not in cited_labels:
                cited_labels.append(label)
            return match.group(0)
        if label not in invalid_labels:
            invalid_labels.append(label)
        # Drop the fabricated marker entirely.
        return ""

    cleaned = _MARKER.sub(_replace, answer_text)
    # Tidy the gaps left by removed markers.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned).strip()

    citations = tuple(
        _build_citation(label, supplied[label]) for label in cited_labels
    )
    return CitationValidation(
        text=cleaned,
        citations=citations,
        invalid_labels=tuple(invalid_labels),
    )


def format_citations(citations: tuple[Citation, ...]) -> list[dict]:
    """Render validated citations as frontend-friendly dictionaries."""
    return [citation.to_dict() for citation in citations]


def citations_for(
    labelled_passages: list[tuple[str, RetrievedPassage]],
) -> CitationValidation:
    """Build a validation with one citation per supplied passage.

    Used by the extractive path, whose plain answer has no inline ``[S1]``
    markers but must still carry its source references. No fabricated marker
    can exist here because the passages are supplied directly.
    """
    citations = tuple(
        _build_citation(label, passage) for label, passage in labelled_passages
    )
    return CitationValidation(text="", citations=citations, invalid_labels=())

