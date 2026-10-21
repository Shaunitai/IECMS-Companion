"""Tests for the IECMS manual assistant, follow-ups and step extraction.

The manual here is a small, clearly-labelled synthetic fixture. It is marked
``verified`` only inside the isolated test corpus so the routing and citation
behaviour can be exercised without the real scanned manual or any network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from chatbot.services import (
    explanation_service,
    intent_service,
    procedure_service,
    retrieval_service,
    safety_service,
    web_search_service,
)
from legal_sources.extraction import _paragraphs_from_ocr_pages
from legal_sources.ocr import OCRPage, OCRLine

from .corpus_fixtures import build_corpus
from .test_pipeline import make_passage


MANUAL_CORPUS = {
    "tst-manual": {
        "title": "TEST FIXTURE - IECMS Getting Started Guide",
        "authority": "TEST FIXTURE - not an official authority",
        "text": (
            "## Filing a Case\n\n"
            "In IECMS, to file a new case:\n\n"
            "Step 1: Click Create New.\n\n"
            "Step 2: Select Case.\n\n"
            "Step 3: Enter the case details.\n\n"
            "## Adding Individual Parties\n\n"
            "To add an individual party:\n\n"
            "Step 1: Open the Party form.\n\n"
            "Step 2: Enter the party details.\n"
        ),
    },
    "tst-affidavit": {
        "title": "TEST FIXTURE - Affidavit",
        "authority": "TEST FIXTURE - not an authority",
        "text": (
            "## Affidavit\n\n"
            "An affidavit is a written statement of facts sworn before a "
            "commissioner of oaths.\n"
        ),
    },
}


class ManualCorpusTestCase(TestCase):
    """Install an isolated verified manual + legal corpus."""

    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.corpus_root = Path(self._tmpdir.name)
        build_corpus(
            self.corpus_root,
            MANUAL_CORPUS,
            status="verified",
            tier=3,
        )
        self._override = override_settings(
            LEGAL_SOURCES_DIR=self.corpus_root,
            ALLOW_SYNTHETIC_SOURCES=False,
            ENABLE_MOCK_CHAT=False,
            ENABLE_WEB_FALLBACK=False,
            ENABLE_IECMS_MANUAL_ASSISTANT=True,
            IECMS_MANUAL_SOURCE_IDS=["tst-manual"],
            # The tiny fixture corpus has low IDF; keep the test focused on
            # routing and citations rather than a production relevance score.
            IECMS_MANUAL_MIN_SCORE=0.5,
            LLM_PROVIDER="",
            LLM_API_KEY="",
        )
        self._override.enable()
        self.addCleanup(self._override.disable)
        retrieval_service.reset_retrieval_service()
        self.addCleanup(retrieval_service.reset_retrieval_service)


class ManualRoutingTests(ManualCorpusTestCase):
    def test_manual_question_is_answered_from_the_manual(self) -> None:
        result = explanation_service.answer_question(
            "How do I file a case in IECMS?", "en"
        )
        self.assertEqual(result["status"], explanation_service.STATUS_EXTRACTIVE)
        self.assertTrue(result["manual"])
        self.assertEqual(result["label"], "IECMS GUIDE")
        self.assertTrue(result["sources"])
        self.assertTrue(
            result["sources"][0]["passage_id"].startswith("tst-manual#")
        )
        self.assertIn("2021", result["notice"])
        self.assertGreaterEqual(len(result["steps"]), 3)
        self.assertIn("Click Create New", result["answer"])

    def test_definition_question_still_uses_the_legal_corpus(self) -> None:
        result = explanation_service.answer_question("What is an affidavit?", "en")
        self.assertFalse(result["manual"])
        self.assertEqual(result["intent"], "legal_definition")
        self.assertTrue(
            result["sources"][0]["passage_id"].startswith("tst-affidavit#")
        )

    def test_implicit_how_to_question_is_offered_to_the_guide_first(self) -> None:
        result = explanation_service.answer_question(
            "How do I enter the case details?", "en"
        )
        self.assertTrue(result["manual"])
        self.assertEqual(result["intent"], "iecms_how_to")
        self.assertIn("2021", result["notice"])

    def test_uncovered_implicit_question_falls_back_to_the_legal_corpus(self) -> None:
        result = explanation_service.answer_question(
            "How do I write an affidavit?", "en"
        )
        self.assertFalse(result["manual"])
        self.assertEqual(result["status"], explanation_service.STATUS_EXTRACTIVE)
        self.assertTrue(
            result["sources"][0]["passage_id"].startswith("tst-affidavit#")
        )


    def test_manual_source_is_excluded_from_the_legal_collection(self) -> None:
        result = retrieval_service.build_retrieval_service().search(
            "How do I file a case in IECMS?"
        )
        self.assertFalse(
            any(passage.source_id == "tst-manual" for passage in result.passages)
        )

    def test_manual_question_searches_the_web_when_the_manual_has_nothing(self) -> None:
        empty = retrieval_service.RetrievalResult(
            query="unsupported", sufficient=False
        )
        no_web = web_search_service.WebSearchOutcome(
            query="unsupported",
            diagnostics=("The search returned no usable results.",),
        )
        with override_settings(ENABLE_WEB_FALLBACK=True), mock.patch.object(
            retrieval_service, "get_manual_retrieval_service"
        ) as get_manual, mock.patch.object(
            web_search_service, "search_and_fetch", return_value=no_web
        ) as fetched:
            get_manual.return_value.search.return_value = empty
            result = explanation_service.answer_question(
                "How do I configure an unsupported IECMS widget?", "en"
            )
        fetched.assert_called_once()
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
        self.assertEqual(result["sources"], [])
        self.assertIn("no usable results", " ".join(result["diagnostics"]))

    def test_explicit_manual_question_uses_the_web_when_manual_is_silent(self) -> None:
        empty = retrieval_service.RetrievalResult(
            query="unsupported", sufficient=False
        )
        page = web_search_service.WebSource(
            title="IECMS calendar help",
            url="https://example.test/calendar",
            host="example.test",
            snippet="",
            passages=(
                "To export the calendar in IECMS, open the Calendar and "
                "choose Export.",
            ),
            retrieved_on="2026-10-19",
        )
        outcome = web_search_service.WebSearchOutcome(
            query="How do I export the calendar in IECMS?", sources=(page,)
        )
        with override_settings(ENABLE_WEB_FALLBACK=True), mock.patch.object(
            retrieval_service, "get_manual_retrieval_service"
        ) as get_manual, mock.patch.object(
            web_search_service, "search_and_fetch", return_value=outcome
        ):
            get_manual.return_value.search.return_value = empty
            result = explanation_service.answer_question(
                "How do I export the calendar in IECMS?", "en"
            )
        self.assertEqual(result["status"], explanation_service.STATUS_EXTRACTIVE)
        self.assertTrue(result["web_fallback"])
        self.assertIn("open-web search", result["notice"])
        self.assertEqual(
            result["sources"][0]["source_url"], "https://example.test/calendar"
        )

    def test_follow_up_keeps_the_manual_context(self) -> None:
        history = [
            {
                "question": "How do I file a case in IECMS?",
                "intent": "iecms_how_to",
            }
        ]
        result = explanation_service.answer_question(
            "How do I add the parties?", "en", history=history
        )
        self.assertTrue(result["is_follow_up"])
        self.assertTrue(result["manual"])
        self.assertEqual(result["intent"], "iecms_how_to")
        self.assertTrue(
            result["sources"][0]["passage_id"].startswith("tst-manual#")
        )

    def test_document_summary_request_is_honest(self) -> None:
        result = explanation_service.answer_question("Summarise this document", "en")
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
        self.assertIn("Upload document", result["answer"])
        self.assertEqual(result["intent"], "document_summary")

    def test_procedural_troubleshooting_is_not_legal_advice(self) -> None:
        question = "What should I do if this step fails?"
        self.assertFalse(
            safety_service.evaluate_question(question, procedural=True).redirect
        )
        self.assertTrue(safety_service.evaluate_question(question).redirect)

        result = explanation_service.answer_question(
            question,
            "en",
            history=[
                {
                    "question": "How do I file a case in IECMS?",
                    "intent": "iecms_how_to",
                }
            ],
        )
        self.assertNotEqual(result["status"], explanation_service.STATUS_REFUSED)

    def test_case_specific_strategy_is_still_redirected_in_manual_context(self) -> None:
        verdict = safety_service.evaluate_question(
            "Should I appeal my case in IECMS?", procedural=True
        )
        self.assertTrue(verdict.redirect)


class IntentTests(SimpleTestCase):
    def test_manual_workflow_questions_are_detected(self) -> None:
        for question in (
            "How do I file a case in IECMS?",
            "How do I reset my password?",
            "How do I add the parties?",
            "How do I sign a document?",
            "How do I merge the documents?",
            "How do I use the calendar?",
        ):
            with self.subTest(question=question):
                self.assertIn(
                    intent_service.classify(question).intent,
                    intent_service.IECMS_INTENTS,
                )

    def test_how_to_without_the_name_is_still_an_iecms_question(self) -> None:
        for question in (
            "How do I register?",
            "How do I pay?",
            "Where do I enter my details?",
        ):
            with self.subTest(question=question):
                result = intent_service.classify(question)
                self.assertIn(result.intent, intent_service.IECMS_INTENTS)
                # It did not name IECMS, so a legal fallback stays possible.
                self.assertFalse(result.explicit_manual)

    def test_workflow_question_without_the_name_is_an_iecms_question(self) -> None:
        result = intent_service.classify("Can I change my password?")
        self.assertIn(result.intent, intent_service.IECMS_INTENTS)
        self.assertFalse(result.explicit_manual)

    def test_definitions_are_not_routed_to_the_manual(self) -> None:
        for question in (
            "What is an affidavit?",
            "What is a case record?",
            "What is a party?",
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    intent_service.classify(question).intent,
                    intent_service.INTENT_LEGAL_DEFINITION,
                )

    def test_short_follow_up_inherits_the_manual_domain(self) -> None:
        history = [
            {"question": "How do I file a case in IECMS?", "intent": "iecms_how_to"}
        ]
        result = intent_service.classify("Explain the next step.", history)
        self.assertTrue(result.is_follow_up)
        self.assertEqual(result.intent, intent_service.INTENT_IECMS_HOW_TO)
        self.assertIn("How do I file a case in IECMS?", result.effective_query)

    def test_self_contained_follow_up_keeps_its_own_retrieval_query(self) -> None:
        history = [
            {"question": "How do I file a case in IECMS?", "intent": "iecms_how_to"}
        ]
        result = intent_service.classify("How do I add the parties?", history)
        self.assertTrue(result.is_follow_up)
        self.assertEqual(result.effective_query, "How do I add the parties?")
        self.assertIn("following up", result.context_note)

    def test_manual_section_question_is_distinguished(self) -> None:
        self.assertEqual(
            intent_service.classify(
                "What does the manual say about filing a case?"
            ).intent,
            intent_service.INTENT_IECMS_MANUAL_SECTION,
        )

    def test_implicit_manual_answers_need_every_meaningful_term(self) -> None:
        registration = make_passage(
            "p1",
            text="REGISTRATION",
            section_heading="Registration",
        )
        self.assertTrue(
            explanation_service._manual_passages_mention(
                [registration], "How do I register?"
            )
        )
        self.assertFalse(
            explanation_service._manual_passages_mention(
                [registration],
                "Tell me about registration of companies",
            )
        )


class ProcedureTests(SimpleTestCase):
    def test_steps_are_extracted_and_prefixes_removed(self) -> None:
        passages = [
            make_passage("p1", text="Step 1: Click Create New."),
            make_passage("p2", text="Step 2: Select Case."),
            make_passage("p3", text="Step 3: Enter the details."),
        ]
        steps = procedure_service.extract_steps_from_passages(passages)
        self.assertEqual(
            steps,
            ["Click Create New.", "Select Case.", "Enter the details."],
        )

    def test_figure_numbers_do_not_count_as_steps(self) -> None:
        text = "Figure 16: Filing a New Case. Section 12.1 applies."
        self.assertEqual(procedure_service.extract_steps(text), [])

    def test_numbering_restart_keeps_the_first_procedure(self) -> None:
        text = "Step 1: One. Step 2: Two. Step 1: Another procedure."
        self.assertEqual(
            procedure_service.extract_steps(text), ["One.", "Two."]
        )


class OCRParagraphTests(SimpleTestCase):
    def _line(self, text: str, *, y: float, height: float = 25.0) -> OCRLine:
        return OCRLine(text=text, x=100.0, y=y, width=600.0, height=height)

    def test_ocr_paragraphs_preserve_page_number_and_heading(self) -> None:
        page = OCRPage(
            page_number=23,
            width=1700.0,
            height=2200.0,
            lines=(
                self._line("Filing a Case", y=300.0, height=40.0),
                self._line("Step 1: Click Create New.", y=360.0),
                self._line("Step 2: Select Case.", y=400.0),
            ),
        )
        paragraphs = _paragraphs_from_ocr_pages((page,))
        self.assertTrue(paragraphs)
        self.assertTrue(all(number == 23 for _, number, _ in paragraphs))
        self.assertTrue(
            any(is_heading and "Filing a Case" in text for text, _, is_heading in paragraphs)
        )
        self.assertTrue(
            any("Step 1" in text for text, _, _ in paragraphs)
        )

    def test_repeated_running_header_is_removed(self) -> None:
        pages = tuple(
            OCRPage(
                page_number=number,
                width=1700.0,
                height=2200.0,
                lines=(
                    self._line("IECMS Case Management System", y=100.0, height=30.0),
                    self._line(f"Body text for page {number}.", y=400.0),
                ),
            )
            for number in range(1, 10)
        )
        paragraphs = _paragraphs_from_ocr_pages(pages)
        self.assertFalse(
            any("Case Management System" in text for text, _, _ in paragraphs)
        )

    def test_two_column_page_is_read_column_by_column(self) -> None:
        """Dictionary and gazette pages must not interleave their columns."""
        def column_line(text, *, x, y, height=25.0):
            return OCRLine(text=text, x=x, y=y, width=600.0, height=height)

        left = [
            column_line("Alpha noun", x=100.0, y=300.0, height=40.0),
            column_line("Alpha collocations line one.", x=100.0, y=350.0),
            column_line("Alpha collocations line two.", x=100.0, y=390.0),
            column_line("Alpha continues here.", x=100.0, y=430.0),
        ]
        right = [
            column_line("Beta noun", x=900.0, y=305.0, height=40.0),
            column_line("Beta collocations line one.", x=900.0, y=355.0),
            column_line("Beta collocations line two.", x=900.0, y=395.0),
            column_line("Beta continues here.", x=900.0, y=435.0),
        ]
        # Interleave the lines in OCR order, as the engine may return them.
        lines = tuple(left[:1] + right[:1] + left[1:] + right[1:])
        page = OCRPage(
            page_number=5, width=1700.0, height=2200.0, lines=lines
        )
        paragraphs = _paragraphs_from_ocr_pages((page,))
        texts = [text for text, _, _ in paragraphs]
        last_alpha = max(
            index for index, text in enumerate(texts) if "Alpha" in text
        )
        first_beta = min(
            index for index, text in enumerate(texts) if "Beta" in text
        )
        self.assertLess(last_alpha, first_beta)
