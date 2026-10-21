"""Phase 3 tests: retrieval, citation validation and grounded explanations.

Provider calls are always mocked: no network access is required and no real
credentials are used.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, override_settings

from chatbot.services import (
    citation_service,
    explanation_service,
    llm_service,
    retrieval_service,
)
from chatbot.services.retrieval_service import RetrievalResult, RetrievedPassage

from .corpus_fixtures import CorpusTestCase, EmptyCorpusTestCase


def make_passage(passage_id: str = "p1", **kwargs) -> RetrievedPassage:
    defaults = {
        "passage_id": passage_id,
        "text": "An affidavit is a sworn written statement of facts.",
        "source_id": "tst-affidavit",
        "document_title": "TEST FIXTURE - Affidavit",
        "issuing_authority": "TEST FIXTURE",
        "document_type": "Test fixture",
        "tier_label": "Synthetic test fixture",
        "verification_status": "synthetic",
        "disclosure": "SYNTHETIC SOURCE - not an official authority.",
    }
    defaults.update(kwargs)
    return RetrievedPassage(**defaults)


class RetrievalTests(CorpusTestCase):
    """Retrieval returns real passages from the approved set."""

    def service(self):
        return retrieval_service.build_retrieval_service()

    def test_known_question_retrieves_the_right_source(self) -> None:
        result = self.service().search("What is an affidavit?")
        self.assertTrue(result.sufficient)
        self.assertTrue(result.passages)
        self.assertEqual(result.passages[0].source_id, "tst-affidavit")

    def test_retrieved_passage_carries_citation_metadata(self) -> None:
        passage = self.service().search("What is an affidavit?").passages[0]
        self.assertTrue(passage.document_title)
        self.assertTrue(passage.issuing_authority)
        self.assertEqual(passage.verification_status, "synthetic")
        self.assertTrue(passage.disclosure)

    def test_unrelated_question_is_not_sufficient(self) -> None:
        result = self.service().search("How do I cook rice?")
        self.assertFalse(result.sufficient)
        self.assertEqual(result.passages, ())

    def test_top_k_limit_is_respected(self) -> None:
        result = self.service().search("court order judgment affidavit", top_k=2)
        self.assertLessEqual(len(result.passages), 2)

    def test_result_reports_diagnostics_when_empty(self) -> None:
        result = self.service().search("zeppelin")
        self.assertFalse(result.sufficient)
        self.assertTrue(result.diagnostics)


class RetrievalAuthorisationTests(CorpusTestCase):
    """Only sources approved for authoritative answers may be returned."""

    allow_synthetic = False

    def test_synthetic_source_is_excluded_when_not_allowed(self) -> None:
        result = retrieval_service.build_retrieval_service().search("What is an affidavit?")
        self.assertFalse(result.sufficient)
        self.assertEqual(result.passages, ())
        self.assertIn("synthetic", result.excluded_statuses)

    def test_verified_source_is_returned_when_synthetic_is_disallowed(self) -> None:
        from legal_sources.ingestion import SourceIngestor
        from legal_sources.paths import CorpusPaths

        SourceIngestor(CorpusPaths.default(self.corpus_root)).mark_verified(
            "tst-affidavit", verified_on="2026-03-03"
        )
        result = retrieval_service.build_retrieval_service().search("What is an affidavit?")
        self.assertTrue(result.sufficient)
        self.assertEqual(result.passages[0].source_id, "tst-affidavit")
        self.assertEqual(result.passages[0].verification_status, "verified")

    def test_pending_source_is_never_authoritative(self) -> None:
        from legal_sources.metadata import SourceMetadata

        metadata = SourceMetadata(
            source_id="x",
            title="x",
            issuing_authority="x",
            verification_status="pending",
        )
        self.assertFalse(metadata.is_authoritative(allow_synthetic=True))


class EmptyCorpusRetrievalTests(EmptyCorpusTestCase):
    def test_empty_corpus_reports_that_it_is_empty(self) -> None:
        result = retrieval_service.build_retrieval_service().search("What is an affidavit?")
        self.assertFalse(result.sufficient)
        self.assertIn("empty", " ".join(result.diagnostics).lower())


class CitationValidationTests(SimpleTestCase):
    """The model must never be able to inject a citation."""

    def setUp(self) -> None:
        super().setUp()
        self.passages = [
            ("S1", make_passage("p1")),
            ("S2", make_passage("p2", source_id="tst-condonation")),
        ]

    def test_valid_markers_are_kept_and_returned(self) -> None:
        result = citation_service.validate_citations(
            "An affidavit is sworn [S1]. Condonation excuses delay [S2].", self.passages
        )
        self.assertEqual([c.label for c in result.citations], ["S1", "S2"])
        self.assertEqual(result.invalid_labels, ())
        self.assertIn("[S1]", result.text)

    def test_fabricated_marker_is_removed(self) -> None:
        result = citation_service.validate_citations(
            "Affidavits are sworn [S1]. This is invented [S9].", self.passages
        )
        self.assertEqual([c.label for c in result.citations], ["S1"])
        self.assertEqual(result.invalid_labels, ("S9",))
        self.assertNotIn("[S9]", result.text)

    def test_answer_with_only_fabricated_markers_yields_no_citations(self) -> None:
        result = citation_service.validate_citations("Invented [S7].", self.passages)
        self.assertFalse(result.has_citations)
        self.assertEqual(result.invalid_labels, ("S7",))
        self.assertNotIn("[S7]", result.text)

    def test_no_markers_yields_no_citations(self) -> None:
        result = citation_service.validate_citations("A plain answer.", self.passages)
        self.assertFalse(result.has_citations)

    def test_duplicate_markers_are_deduplicated(self) -> None:
        result = citation_service.validate_citations("[S1] and again [S1]", self.passages)
        self.assertEqual([c.label for c in result.citations], ["S1"])

    def test_empty_answer_is_rejected(self) -> None:
        with self.assertRaises(citation_service.CitationValidationError):
            citation_service.validate_citations("", self.passages)

    def test_synthetic_flag_is_exposed(self) -> None:
        result = citation_service.validate_citations("[S1]", self.passages)
        self.assertTrue(result.citations[0].is_synthetic)
        self.assertFalse(result.citations[0].is_authoritative)
        self.assertIn("synthetic", result.to_dicts()[0]["verification_status"])


class LLMServiceUnitTests(SimpleTestCase):
    """Provider configuration and failure classification."""

    def test_missing_credentials_is_reported_not_guessed(self) -> None:
        with self.assertRaises(llm_service.LLMServiceError) as ctx:
            llm_service.generate_explanation(
                question="q",
                labelled_passages=[("S1", make_passage())],
                language="en",
                system_instruction="sys",
            )
        self.assertEqual(ctx.exception.code, "not_configured")

    @override_settings(LLM_API_KEY="test-key")
    def test_missing_evidence_is_refused(self) -> None:
        with self.assertRaises(llm_service.LLMServiceError) as ctx:
            llm_service.generate_explanation(
                question="q",
                labelled_passages=[],
                language="en",
                system_instruction="sys",
            )
        self.assertEqual(ctx.exception.code, "no_evidence")

    def test_error_classification(self) -> None:
        class FakeClientError(Exception):
            pass

        FakeClientError.__name__ = "ClientError"

        rate_limited = FakeClientError()
        rate_limited.code = 429
        self.assertEqual(llm_service._classify_error(rate_limited), ("rate_limited", True))

        auth = FakeClientError()
        auth.code = 403
        self.assertEqual(
            llm_service._classify_error(auth), ("authentication_failed", False)
        )

        self.assertEqual(
            llm_service._classify_error(TimeoutError()), ("timeout", True)
        )

    def test_prompt_labels_every_source_and_forbids_outside_knowledge(self) -> None:
        prompt = llm_service.build_user_prompt(
            "What is an affidavit?", [("S1", make_passage())]
        )
        self.assertIn("[S1]", prompt)
        self.assertIn("QUESTION: What is an affidavit?", prompt)
        self.assertIn("using only the sources above", prompt)

    def test_config_reports_credentials_without_exposing_them(self) -> None:
        with override_settings(LLM_API_KEY=""):
            self.assertFalse(llm_service.get_llm_config().has_credentials)
        with override_settings(LLM_API_KEY="secret-value"):
            config = llm_service.get_llm_config()
            self.assertTrue(config.has_credentials)
            self.assertNotIn("secret-value", repr(config.model))


class ParagraphFormattingTests(SimpleTestCase):
    """Quoted document text must read as a paragraph, not printed lines."""

    def test_hard_wrapped_lines_are_joined_into_one_paragraph(self) -> None:
        text = (
            "S.I. 202 of 2021\n1097\nStatutory Instrument 202 of 2021.\n"
            "[CAP. 7:06"
        )
        self.assertEqual(
            explanation_service._as_paragraph(text),
            "S.I. 202 of 2021 1097 Statutory Instrument 202 of 2021. [CAP. 7:06",
        )

    def test_hyphenated_word_split_across_lines_is_rejoined(self) -> None:
        self.assertEqual(
            explanation_service._as_paragraph(
                "An application for condo-\nnation may be made."
            ),
            "An application for condonation may be made.",
        )

    def test_unreadable_glyphs_are_removed_without_joining_words(self) -> None:
        self.assertEqual(
            explanation_service._as_paragraph(
                "labour rights\uf0a8); and\nassociated duties"
            ),
            "labour rights); and associated duties",
        )


class ExtractiveExplanationTests(CorpusTestCase):
    """With no provider configured, answers quote the source and cite it."""

    def test_grounded_answer_is_returned_with_citations(self) -> None:
        result = explanation_service.answer_question("What is an affidavit?", "en")
        self.assertEqual(result["status"], explanation_service.STATUS_EXTRACTIVE)
        self.assertTrue(result["sources"])
        self.assertTrue(result["synthetic_warning"])
        self.assertIn("affidavit", result["answer"].lower())

    def test_source_metadata_is_included(self) -> None:
        source = explanation_service.answer_question("What is an affidavit?", "en")["sources"][0]
        self.assertIn("document_title", source)
        self.assertIn("verification_status", source)
        self.assertIn("disclosure", source)
        self.assertTrue(source["synthetic"])

    def test_citation_exposes_a_local_document_url(self) -> None:
        passage = make_passage("p1", local_path="/tmp/affidavit.pdf")
        validation = citation_service.citations_for([("S1", passage)])
        self.assertEqual(
            validation.citations[0].local_url, "/sources/tst-affidavit/"
        )
        self.assertEqual(
            validation.to_dicts()[0]["local_url"], "/sources/tst-affidavit/"
        )

    def test_citation_without_a_local_copy_has_no_local_url(self) -> None:
        passage = make_passage("p1", source_url="https://example.test/doc")
        validation = citation_service.citations_for([("S1", passage)])
        self.assertEqual(validation.citations[0].local_url, "")

    def test_advice_request_is_flagged_and_redirected(self) -> None:
        # A question that both asks for case-specific help and matches a source.
        result = explanation_service.answer_question(
            "Should I apply for condonation in my case?", "en"
        )
        self.assertTrue(result["redirected"])
        self.assertIn("can't advise you on your own case", result["answer"])
        self.assertTrue(result["sources"])

    def test_advice_request_without_evidence_is_still_flagged(self) -> None:
        result = explanation_service.answer_question("Should I appeal my eviction?", "en")
        self.assertTrue(result["redirected"])
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)

    def test_unknown_topic_yields_insufficient_evidence(self) -> None:
        result = explanation_service.answer_question("How do I cook rice?", "en")
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
        self.assertEqual(result["sources"], [])

    def test_incidental_word_match_is_not_used_as_evidence(self) -> None:
        """A passage that shares one stray word must not ground an answer."""
        weak = RetrievalResult(
            query="Tell me about registration of companies",
            passages=(make_passage("p1", text="Registration of a charge."),),
            sufficient=True,
            confidence="low",
        )
        with override_settings(ENABLE_WEB_FALLBACK=False), mock.patch.object(
            retrieval_service, "get_retrieval_service"
        ) as get_service:
            get_service.return_value.search.return_value = weak
            result = explanation_service.answer_question(
                "Tell me about registration of companies", "en"
            )
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
        self.assertEqual(result["sources"], [])
        self.assertIn("will not guess", result["answer"])


class ProviderBackedExplanationTests(CorpusTestCase):
    """The LLM path, with the provider mocked out."""

    def answer(self, text: str, **kwargs):
        generated = llm_service.LLMAnswer(
            text=text, model="test-model", provider="gemini", attempts=1
        )
        with mock.patch.object(llm_service, "is_configured", return_value=True), mock.patch.object(
            llm_service, "generate_explanation", return_value=generated
        ) as patched:
            result = explanation_service.answer_question(
                kwargs.pop("question", "What is an affidavit?"), kwargs.pop("language", "en")
            )
        return result, patched

    def test_valid_cited_answer_is_returned(self) -> None:
        result, _ = self.answer("An affidavit is sworn evidence [S1].")
        self.assertEqual(result["status"], explanation_service.STATUS_OK)
        self.assertEqual(result["model"], "test-model")
        self.assertEqual([s["label"] for s in result["sources"]], ["S1"])

    def test_fabricated_citation_is_dropped(self) -> None:
        result, _ = self.answer("Sworn evidence [S1]. Also invented [S42].")
        labels = [s["label"] for s in result["sources"]]
        self.assertEqual(labels, ["S1"])
        self.assertNotIn("[S42]", result["answer"])
        self.assertEqual(result["dropped_citations"], 1)

    def test_answer_with_no_valid_citation_is_not_shown_as_grounded(self) -> None:
        result, _ = self.answer("I think it is roughly like a statement.")
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)

    def test_provider_failure_is_reported_clearly(self) -> None:
        with mock.patch.object(llm_service, "is_configured", return_value=True), mock.patch.object(
            llm_service,
            "generate_explanation",
            side_effect=llm_service.LLMServiceError("Busy", code="rate_limited"),
        ):
            result = explanation_service.answer_question("What is an affidavit?", "en")
        self.assertEqual(result["status"], explanation_service.STATUS_PROVIDER_ERROR)
        self.assertEqual(result["error_code"], "rate_limited")
        self.assertEqual(result["sources"], [])

    def test_system_instructions_are_passed_to_the_provider(self) -> None:
        _, patched = self.answer("Sworn evidence [S1].")
        _, kwargs = patched.call_args
        self.assertIn("UNTRUSTED INPUT", kwargs["system_instruction"])


class NoEvidenceNeverReachesProviderTests(EmptyCorpusTestCase):
    def test_provider_is_not_called_without_evidence(self) -> None:
        with mock.patch.object(llm_service, "generate_explanation") as patched:
            result = explanation_service.answer_question("What is an affidavit?", "en")
        patched.assert_not_called()
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
