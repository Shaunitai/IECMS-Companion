"""Source metadata and verification model.

Every document in the knowledge base carries this metadata. The verification
status decides whether a document may support an authoritative explanation, and
it is never set automatically: a human (or an explicitly approved process) must
mark a document as ``verified``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class VerificationStatus(str, Enum):
    """How much trust a document has earned."""

    VERIFIED = "verified"
    PENDING = "pending"
    OUTDATED = "outdated"
    SUPERSEDED = "superseded"
    INACCESSIBLE = "inaccessible"
    UNVERIFIED = "unverified"
    SYNTHETIC = "synthetic"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


class SourceTier:
    """Source priority from the source policy (lower number = higher priority)."""

    LEGISLATION = 1
    COURT_RULES = 2
    JSC_PUBLICATION = 3
    OTHER_APPROVED = 4
    SYNTHETIC = 99

    ALL = (LEGISLATION, COURT_RULES, JSC_PUBLICATION, OTHER_APPROVED, SYNTHETIC)


# Human-readable labels used in the interface.
TIER_LABELS = {
    SourceTier.LEGISLATION: "Legislation",
    SourceTier.COURT_RULES: "Court rules or practice direction",
    SourceTier.JSC_PUBLICATION: "JSC publication",
    SourceTier.OTHER_APPROVED: "Other approved source",
    SourceTier.SYNTHETIC: "Synthetic test fixture",
}

# Statuses that may ground an authoritative explanation when synthetic sources
# are permitted. ``SYNTHETIC`` is only usable when the caller explicitly allows
# it, and the answer must always disclose it.
AUTHORITATIVE_STATUSES = frozenset({VerificationStatus.VERIFIED})


@dataclass(frozen=True)
class SourceMetadata:
    """Descriptive and governance metadata for one knowledge-base document."""

    source_id: str
    title: str
    issuing_authority: str
    source_url: str = ""
    local_path: str = ""
    document_type: str = ""
    court_or_jurisdiction: str = "Zimbabwe"
    tier: int = SourceTier.OTHER_APPROVED
    publication_date: str | None = None
    effective_date: str | None = None
    end_date: str | None = None
    last_verified: str | None = None
    version_info: str = ""
    verification_status: str = VerificationStatus.UNVERIFIED.value
    # Opt-in OCR for scanned PDFs. It is off by default so that a large scanned
    # statute is not OCR'd merely because it was ingested; a manifest enables it
    # explicitly for a source whose text layer is known to be missing.
    ocr: bool = False
    extraction_warnings: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    # -- Convenience ------------------------------------------------------

    @property
    def status(self) -> VerificationStatus:
        try:
            return VerificationStatus(self.verification_status)
        except ValueError:
            return VerificationStatus.UNVERIFIED

    @property
    def is_synthetic(self) -> bool:
        return self.status is VerificationStatus.SYNTHETIC

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, "Unclassified source")

    def is_authoritative(self, *, allow_synthetic: bool = False) -> bool:
        """Whether this document may support an authoritative explanation.

        Unknown currency is never treated as current: only an explicit
        ``verified`` status qualifies, plus ``synthetic`` when the caller has
        explicitly opted in.
        """
        if self.status in AUTHORITATIVE_STATUSES:
            return True
        if allow_synthetic and self.is_synthetic:
            return True
        return False

    def disclosure(self, *, allow_synthetic: bool = False) -> str:
        """A short, honest statement about this source's standing."""
        text = self._disclosure_text(allow_synthetic=allow_synthetic)
        if any(
            "OCR was applied" in warning
            for warning in self.extraction_warnings
        ):
            text = (
                f"{text} Text was recovered by OCR; recognition errors are "
                "possible."
            )
        return text

    def _disclosure_text(self, *, allow_synthetic: bool = False) -> str:
        if self.status is VerificationStatus.VERIFIED:
            verified = self.last_verified or "an unrecorded date"
            return f"Verified official source (last verified {verified})."
        if self.is_synthetic:
            if allow_synthetic:
                return (
                    "SYNTHETIC SOURCE \u2014 a software test fixture, not an "
                    "official Zimbabwean legal authority."
                )
            return (
                "Synthetic test fixture excluded from authoritative answers."
            )
        if self.status is VerificationStatus.PENDING:
            return "Retrieved but not yet verified; currency not established."
        if self.status is VerificationStatus.OUTDATED:
            return "Outdated: a later version is believed to exist."
        if self.status is VerificationStatus.SUPERSEDED:
            return "Superseded by a later instrument."
        if self.status is VerificationStatus.INACCESSIBLE:
            return "Referenced but could not be retrieved; contents unverified."
        return "Provenance and currency unverified."

    # -- Serialisation ----------------------------------------------------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["extraction_warnings"] = list(self.extraction_warnings)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SourceMetadata":
        known = {f for f in cls.__dataclass_fields__}
        payload = {key: value for key, value in data.items() if key in known}
        if "extraction_warnings" in payload:
            payload["extraction_warnings"] = tuple(payload["extraction_warnings"] or ())
        return cls(**payload)


def load_registry(path: str | Path) -> dict[str, SourceMetadata]:
    """Load the source registry keyed by ``source_id``.

    A missing registry is not an error: it simply means no documents have been
    ingested yet.
    """
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = raw.get("sources", raw if isinstance(raw, list) else [])
    registry: dict[str, SourceMetadata] = {}
    for entry in sources:
        metadata = SourceMetadata.from_dict(entry)
        registry[metadata.source_id] = metadata
    return registry


def save_registry(path: str | Path, registry: dict[str, SourceMetadata]) -> None:
    """Write the source registry as stable, readable JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "sources": [registry[key].to_dict() for key in sorted(registry)],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
