"""Phase 2 tests: metadata, extraction, ingestion and local indexing.

All documents created here are synthetic test fixtures.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase

from legal_sources.extraction import ExtractedDocument, extract
from legal_sources.indexing import KeywordIndex, IndexedPassage, tokenize
from legal_sources.ingestion import (
    FetchResult,
    SourceIngestionError,
    SourceIngestor,
)
from legal_sources.manifests import load_manifest
from legal_sources.metadata import (
    SourceMetadata,
    SourceTier,
    VerificationStatus,
    load_registry,
    save_registry,
)
from legal_sources.paths import CorpusPaths

from .corpus_fixtures import SYNTHETIC_CORPUS, build_corpus


class TempDirMixin:
    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)


class SourceMetadataTests(SimpleTestCase):
    """Source tiering, verification and honest disclosure."""

    def metadata(self, **kwargs) -> SourceMetadata:
        base = {
            "source_id": "s1",
            "title": "A document",
            "issuing_authority": "An authority",
        }
        base.update(kwargs)
        return SourceMetadata(**base)

    def test_unverified_is_not_authoritative(self) -> None:
        self.assertFalse(self.metadata().is_authoritative())

    def test_pending_is_not_authoritative(self) -> None:
        meta = self.metadata(verification_status=VerificationStatus.PENDING.value)
        self.assertFalse(meta.is_authoritative())
        self.assertIn("not yet verified", meta.disclosure())

    def test_only_verified_is_authoritative_by_default(self) -> None:
        meta = self.metadata(
            verification_status=VerificationStatus.VERIFIED.value,
            last_verified="2026-01-01",
        )
        self.assertTrue(meta.is_authoritative())
        self.assertIn("2026-01-01", meta.disclosure())

    def test_synthetic_requires_explicit_opt_in(self) -> None:
        meta = self.metadata(verification_status=VerificationStatus.SYNTHETIC.value)
        self.assertFalse(meta.is_authoritative())
        self.assertTrue(meta.is_authoritative(allow_synthetic=True))
        self.assertIn("SYNTHETIC", meta.disclosure(allow_synthetic=True))

    def test_unknown_status_falls_back_to_unverified(self) -> None:
        meta = self.metadata(verification_status="banana")
        self.assertIs(meta.status, VerificationStatus.UNVERIFIED)

    def test_ocr_sources_disclose_that_text_was_recognised(self) -> None:
        meta = self.metadata(
            verification_status=VerificationStatus.VERIFIED.value,
            last_verified="2026-10-19",
            extraction_warnings=(
                "No text layer found; the PDF may be a scanned image and "
                "would need OCR.",
                "OCR was applied to the scanned pages. OCR text may contain "
                "recognition errors; page numbers refer to the PDF pages.",
            ),
        )
        self.assertIn("OCR", meta.disclosure())

    def test_text_sources_do_not_claim_ocr(self) -> None:
        meta = self.metadata(
            verification_status=VerificationStatus.VERIFIED.value,
            last_verified="2026-10-19",
        )
        self.assertNotIn("OCR", meta.disclosure())

    def test_tier_labels(self) -> None:
        self.assertEqual(
            self.metadata(tier=SourceTier.LEGISLATION).tier_label, "Legislation"
        )
        self.assertEqual(
            self.metadata(tier=SourceTier.SYNTHETIC).tier_label,
            "Synthetic test fixture",
        )

    def test_round_trip_through_dict(self) -> None:
        meta = self.metadata(
            verification_status=VerificationStatus.VERIFIED.value,
            extraction_warnings=("page 2 unreadable",),
        )
        restored = SourceMetadata.from_dict(meta.to_dict())
        self.assertEqual(restored, meta)

    def test_registry_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            registry = {"s1": self.metadata()}
            save_registry(path, registry)
            self.assertEqual(load_registry(path)["s1"].source_id, "s1")

    def test_missing_registry_is_empty_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_registry(Path(tmp) / "nope.json"), {})


class ExtractionTests(TempDirMixin, SimpleTestCase):
    """Text extraction preserves page and heading references where possible."""

    def test_markdown_headings_are_captured(self) -> None:
        path = self.root / "doc.md"
        path.write_text(
            "## Affidavit\n\nAn affidavit is a sworn written statement.\n",
            encoding="utf-8",
        )
        document = extract("doc", path)
        self.assertTrue(document.blocks)
        self.assertEqual(document.blocks[0].heading, "Affidavit")

    def test_hard_wrapped_lines_are_flattened_into_prose(self) -> None:
        """A PDF text layer breaks lines at the printed page width."""
        from legal_sources.extraction import _normalise

        self.assertEqual(
            _normalise("An application for condo-\nnation\nmay be made."),
            "An application for condonation may be made.",
        )

    def test_warning_banner_is_not_treated_as_a_heading(self) -> None:
        """Regression guard.

        A bold uppercase Markdown warning line was being detected as a section
        heading, so citations displayed the banner instead of the real heading.
        """
        path = self.root / "doc.md"
        path.write_text(
            "> **WARNING - SYNTHETIC SOURCE.**\n\n"
            "## Affidavit\n\n"
            "An affidavit is a written statement of facts sworn before a "
            "commissioner of oaths.\n",
            encoding="utf-8",
        )
        document = extract("doc", path)
        headings = {block.heading for block in document.blocks}
        self.assertIn("Affidavit", headings)
        self.assertFalse(
            any("WARNING" in heading for heading in headings),
            f"warning banner leaked into headings: {headings}",
        )

    def test_uppercase_part_heading_is_detected_in_plain_text(self) -> None:
        path = self.root / "doc.txt"
        path.write_text(
            "PART A - GENERAL\n\nAn order of court is a direction made by a court "
            "requiring a party to do or refrain from doing something.\n",
            encoding="utf-8",
        )
        document = extract("doc", path)
        self.assertEqual(document.blocks[0].heading, "PART A - GENERAL")

    def test_ordinary_sentences_are_not_headings(self) -> None:
        path = self.root / "doc.txt"
        path.write_text(
            "A notice of set down places a matter on the roll for hearing.\n",
            encoding="utf-8",
        )
        document = extract("doc", path)
        self.assertEqual(document.blocks[0].heading, "")

    def test_html_extraction_ignores_scripts(self) -> None:
        path = self.root / "doc.html"
        path.write_text(
            "<html><body><script>alert('x')</script>"
            "<h2>Notice of Set Down</h2>"
            "<p>A notice of set down places a matter on the roll for hearing.</p>"
            "</body></html>",
            encoding="utf-8",
        )
        document = extract("doc", path)
        text = " ".join(block.text for block in document.blocks)
        self.assertIn("notice of set down places a matter", text.lower())
        self.assertNotIn("alert", text)

    def test_docx_extraction(self) -> None:
        import docx

        path = self.root / "doc.docx"
        document = docx.Document()
        document.add_heading("Default Judgment", level=2)
        document.add_paragraph(
            "A default judgment is given when a party fails to take a required step."
        )
        document.save(str(path))

        extracted = extract("doc", path)
        self.assertTrue(extracted.blocks)
        self.assertIn("default judgment", " ".join(b.text for b in extracted.blocks).lower())

    def test_blank_pdf_reports_probable_scan(self) -> None:
        from pypdf import PdfWriter

        path = self.root / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with path.open("wb") as handle:
            writer.write(handle)

        extracted = extract("scan", path)
        self.assertEqual(extracted.blocks, [])
        self.assertTrue(
            any("scanned" in warning.lower() for warning in extracted.warnings),
            extracted.warnings,
        )

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            extract("doc", self.root / "missing.txt")

    def test_unsupported_suffix_raises(self) -> None:
        path = self.root / "doc.rtf"
        path.write_text("hello", encoding="utf-8")
        with self.assertRaises(ValueError):
            extract("doc", path)

    def test_extracted_document_round_trip(self) -> None:
        path = self.root / "doc.md"
        path.write_text(
            "## Affidavit\n\nAn affidavit is a written statement of facts that the "
            "deponent swears before a commissioner of oaths.\n",
            encoding="utf-8",
        )
        document = extract("doc", path)
        restored = ExtractedDocument.from_dict(document.to_dict())
        self.assertEqual(restored.blocks, document.blocks)


class TokenizerTests(SimpleTestCase):
    def test_stopwords_are_removed(self) -> None:
        self.assertEqual(tokenize("What is the affidavit?"), ["affidavit"])

    def test_legal_terms_survive(self) -> None:
        self.assertIn("order", tokenize("an order of court"))
        self.assertIn("notice", tokenize("a notice of set down"))


class KeywordIndexTests(TempDirMixin, SimpleTestCase):
    def build(self) -> KeywordIndex:
        build_corpus(self.root)
        paths = CorpusPaths.default(self.root)
        return KeywordIndex.load(paths.index_file)

    def test_index_is_built_with_passages(self) -> None:
        index = self.build()
        self.assertGreater(index.size, 0)
        self.assertEqual(len(index.source_ids), len(SYNTHETIC_CORPUS))

    def test_known_term_retrieves_expected_source(self) -> None:
        index = self.build()
        hits = index.search("What is an affidavit?")
        self.assertTrue(hits)
        self.assertEqual(hits[0].passage.source_id, "tst-affidavit")

    def test_condonation_query_retrieves_condonation(self) -> None:
        index = self.build()
        hits = index.search("application for condonation")
        self.assertEqual(hits[0].passage.source_id, "tst-condonation")

    def test_unknown_term_returns_nothing(self) -> None:
        index = self.build()
        self.assertEqual(index.search("zeppelin"), [])

    def test_empty_index_search_is_empty(self) -> None:
        self.assertEqual(KeywordIndex().search("affidavit"), [])

    def test_save_and_load_preserve_results(self) -> None:
        index = self.build()
        before = [hit.passage.passage_id for hit in index.search("affidavit")]
        paths = CorpusPaths.default(self.root)
        reloaded = KeywordIndex.load(paths.index_file)
        after = [hit.passage.passage_id for hit in reloaded.search("affidavit")]
        self.assertEqual(before, after)

    def test_add_and_search_single_passage(self) -> None:
        index = KeywordIndex()
        index.add(
            IndexedPassage(
                passage_id="p1",
                source_id="s1",
                text="A subpoena compels a witness to attend court.",
            )
        )
        hits = index.search("subpoena witness")
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0].score, 0)


class IngestionTests(TempDirMixin, TestCase):
    def make_ingestor(self, **kwargs) -> SourceIngestor:
        defaults = {"allowed_hosts": ["approved.test"]}
        defaults.update(kwargs)
        return SourceIngestor(CorpusPaths.default(self.root), **defaults)

    def test_local_ingestion_registers_and_extracts(self) -> None:
        document_path = self.root / "affidavit.md"
        document_path.write_text(
            "## Affidavit\n\nAn affidavit is a sworn written statement of facts "
            "made before a commissioner of oaths.\n",
            encoding="utf-8",
        )
        ingestor = self.make_ingestor()
        metadata = ingestor.ingest_local_file(
            document_path,
            SourceMetadata(
                source_id="aff",
                title="Affidavit fixture",
                issuing_authority="TEST FIXTURE",
            ),
        )

        self.assertEqual(metadata.verification_status, VerificationStatus.UNVERIFIED.value)
        self.assertTrue(CorpusPaths.default(self.root).extracted_path("aff").exists())
        self.assertIn("aff", load_registry(CorpusPaths.default(self.root).registry))

    def test_extraction_warnings_are_merged_into_metadata(self) -> None:
        from pypdf import PdfWriter

        pdf_path = self.root / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with pdf_path.open("wb") as handle:
            writer.write(handle)

        metadata = self.make_ingestor().ingest_local_file(
            pdf_path,
            SourceMetadata(
                source_id="scan", title="Scan", issuing_authority="TEST FIXTURE"
            ),
        )
        self.assertTrue(metadata.extraction_warnings)

    def test_missing_local_file_raises(self) -> None:
        with self.assertRaises(SourceIngestionError):
            self.make_ingestor().ingest_local_file(
                self.root / "nope.md",
                SourceMetadata(source_id="x", title="x", issuing_authority="x"),
            )

    def test_non_http_scheme_is_refused(self) -> None:
        with self.assertRaises(SourceIngestionError):
            self.make_ingestor().fetch_url("file:///C:/Windows/win.ini")

    def test_host_outside_allow_list_is_refused(self) -> None:
        with self.assertRaises(SourceIngestionError):
            self.make_ingestor().fetch_url("https://evil.test/page")

    def test_robots_refusal_marks_source_inaccessible(self) -> None:
        ingestor = self.make_ingestor()
        with mock.patch.object(
            SourceIngestor, "_robots_allow", return_value=(False, "robots.txt disallows this URL.")
        ):
            metadata = ingestor.ingest_url(
                "https://approved.test/page",
                SourceMetadata(
                    source_id="web1", title="Web", issuing_authority="TEST FIXTURE"
                ),
            )
        self.assertEqual(
            metadata.verification_status, VerificationStatus.INACCESSIBLE.value
        )
        self.assertIn("robots.txt", " ".join(metadata.extraction_warnings))

    def test_unreadable_robots_txt_is_treated_as_disallowed(self) -> None:
        ingestor = self.make_ingestor()
        with mock.patch("urllib.robotparser.RobotFileParser.read", side_effect=OSError("boom")):
            allowed, reason = ingestor._robots_allow("https://approved.test/page")
        self.assertFalse(allowed)
        self.assertIn("refusing", reason.lower())

    def test_failed_fetch_marks_source_inaccessible_without_content(self) -> None:
        ingestor = self.make_ingestor()
        with mock.patch.object(
            SourceIngestor,
            "fetch_url",
            return_value=FetchResult(
                url="https://approved.test/x", status=503, content_type="", error="HTTP 503"
            ),
        ):
            metadata = ingestor.ingest_url(
                "https://approved.test/x",
                SourceMetadata(
                    source_id="web2", title="Web 2", issuing_authority="TEST FIXTURE"
                ),
            )
        self.assertEqual(
            metadata.verification_status, VerificationStatus.INACCESSIBLE.value
        )
        self.assertIn("HTTP 503", " ".join(metadata.extraction_warnings))
        # No extracted file should have been written for an inaccessible source.
        self.assertFalse(CorpusPaths.default(self.root).extracted_path("web2").exists())

    def test_mark_verified_requires_a_manual_call(self) -> None:
        document_path = self.root / "doc.md"
        document_path.write_text(
            "## Order of Court\n\nAn order is a direction made by a court.\n",
            encoding="utf-8",
        )
        ingestor = self.make_ingestor()
        ingestor.ingest_local_file(
            document_path,
            SourceMetadata(source_id="ord", title="Order", issuing_authority="TEST FIXTURE"),
        )
        self.assertFalse(ingestor.registry["ord"].is_authoritative())

        updated = ingestor.mark_verified("ord", verified_on="2026-02-02")
        self.assertEqual(updated.verification_status, VerificationStatus.VERIFIED.value)
        self.assertEqual(updated.last_verified, "2026-02-02")

    def test_mark_verified_rejects_unknown_source(self) -> None:
        with self.assertRaises(SourceIngestionError):
            self.make_ingestor().mark_verified("ghost")


class ManifestTests(SimpleTestCase):
    def test_bundled_manifest_is_marked_synthetic(self) -> None:
        from django.conf import settings

        manifest = load_manifest(Path(settings.LEGAL_SOURCES_DIR) / "manifest.json")
        self.assertTrue(manifest.synthetic)
        self.assertTrue(manifest.entries)
        for entry in manifest.entries:
            self.assertEqual(
                entry.metadata.verification_status,
                VerificationStatus.SYNTHETIC.value,
                f"{entry.source_id} must be marked synthetic",
            )

    def test_bundled_manifest_paths_exist(self) -> None:
        from django.conf import settings

        manifest = load_manifest(Path(settings.LEGAL_SOURCES_DIR) / "manifest.json")
        for entry in manifest.entries:
            self.assertIsNotNone(entry.path)
            self.assertTrue(entry.path.exists(), f"missing fixture: {entry.path}")

    def test_missing_manifest_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_manifest(Path(tmp) / "nope.json")
