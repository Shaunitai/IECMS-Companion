"""Helpers for building an isolated test corpus.

Every document created here is a **synthetic test fixture**. Nothing in this
module represents real law, a real instrument or a real authority.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from chatbot.services.retrieval_service import reset_retrieval_service
from legal_sources.ingestion import SourceIngestor
from legal_sources.metadata import SourceMetadata
from legal_sources.paths import CorpusPaths

# Synthetic fixtures used across the test suite. Kept small and deliberately
# unambiguous so retrieval assertions are stable. Every fixture states its own
# status in its text as well as in its metadata, so a retrieved passage cannot
# be mistaken for real law.
SYNTHETIC_DECLARATION = (
    "SYNTHETIC TEST FIXTURE: this document is not an official legal authority "
    "and exists only for software testing.\n\n"
)

SYNTHETIC_CORPUS: dict[str, dict[str, str]] = {
    "tst-affidavit": {
        "title": "TEST FIXTURE - Affidavit",
        "authority": "TEST FIXTURE - not an authority",
        "text": SYNTHETIC_DECLARATION + (
            "## Affidavit\n\n"
            "An affidavit is a written statement of facts that the deponent "
            "swears or affirms to be true before a commissioner of oaths. The "
            "jurat records that the statement was sworn.\n\n"
            "## Contents\n\n"
            "An affidavit normally identifies the deponent, sets out the facts "
            "in numbered paragraphs, and ends with a signature.\n"
        ),
    },
    "tst-condonation": {
        "title": "TEST FIXTURE - Application for Condonation",
        "authority": "TEST FIXTURE - not an authority",
        "text": SYNTHETIC_DECLARATION + (
            "## Application for Condonation\n\n"
            "Condonation is the court's power to excuse a party's failure to "
            "comply with a time limit. An application for condonation asks the "
            "court to overlook the non-compliance so the matter may proceed.\n\n"
            "The applicant normally explains the delay and the prospects of "
            "success.\n"
        ),
    },
    "tst-default-judgment": {
        "title": "TEST FIXTURE - Default Judgment",
        "authority": "TEST FIXTURE - not an authority",
        "text": SYNTHETIC_DECLARATION + (
            "## Default Judgment\n\n"
            "A default judgment is a judgment given because the other party "
            "failed to take a required step, such as filing a defence, within "
            "the time allowed.\n"
        ),
    },
}


def build_corpus(
    root: str | Path,
    corpus: dict[str, dict[str, str]] | None = None,
    *,
    status: str = "synthetic",
    tier: int = 99,
) -> CorpusPaths:
    """Create documents, ingest them and build an index under ``root``."""
    paths = CorpusPaths.default(root)
    paths.ensure_directories()
    ingestor = SourceIngestor(paths)

    for source_id, spec in (corpus or SYNTHETIC_CORPUS).items():
        document_path = paths.root / f"{source_id}.md"
        document_path.write_text(spec["text"], encoding="utf-8")
        metadata = SourceMetadata(
            source_id=source_id,
            title=spec.get("title", source_id),
            issuing_authority=spec.get("authority", "TEST FIXTURE"),
            document_type=spec.get("document_type", "Test fixture"),
            tier=tier,
            verification_status=status,
            local_path=str(document_path),
            last_verified="2026-01-01" if status == "verified" else None,
        )
        ingestor.ingest_local_file(document_path, metadata)

    ingestor.rebuild_index()
    return paths


class CorpusTestCase(TestCase):
    """Base case that installs an isolated, temporary corpus for each test."""

    corpus_status = "synthetic"
    allow_synthetic = True

    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.corpus_root = Path(self._tmpdir.name)
        build_corpus(self.corpus_root, status=self.corpus_status)

        self._override = override_settings(
            LEGAL_SOURCES_DIR=self.corpus_root,
            ALLOW_SYNTHETIC_SOURCES=self.allow_synthetic,
            ENABLE_MOCK_CHAT=False,
        )
        self._override.enable()
        self.addCleanup(self._override.disable)

        reset_retrieval_service()
        self.addCleanup(reset_retrieval_service)


class EmptyCorpusTestCase(TestCase):
    """Base case with no knowledge base at all (the production default)."""

    allow_synthetic = False

    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.corpus_root = Path(self._tmpdir.name)

        self._override = override_settings(
            LEGAL_SOURCES_DIR=self.corpus_root,
            ALLOW_SYNTHETIC_SOURCES=self.allow_synthetic,
            ENABLE_MOCK_CHAT=False,
        )
        self._override.enable()
        self.addCleanup(self._override.disable)

        reset_retrieval_service()
        self.addCleanup(reset_retrieval_service)
