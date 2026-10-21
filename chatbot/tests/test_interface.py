"""Interface, routing and input-validation tests.

Uses an empty knowledge base so the tests never depend on whatever corpus the
developer happens to have indexed locally.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from chatbot.services import summarisation_service
from chatbot.services.summarisation_service import SummaryResult
from legal_sources.extraction import ExtractedBlock, ExtractedDocument
from legal_sources.metadata import SourceMetadata, save_registry
from legal_sources.paths import CorpusPaths

from .corpus_fixtures import EmptyCorpusTestCase


class IndexPageTests(EmptyCorpusTestCase):
    """The interface shell renders and states its purpose and limits."""

    def setUp(self) -> None:
        super().setUp()
        self.response = self.client.get(reverse("chatbot:index"))

    def test_index_returns_ok(self) -> None:
        self.assertEqual(self.response.status_code, 200)

    def test_index_uses_expected_templates(self) -> None:
        self.assertTemplateUsed(self.response, "chatbot/index.html")
        self.assertTemplateUsed(self.response, "chatbot/base.html")

    def test_index_shows_app_name_and_purpose(self) -> None:
        self.assertContains(self.response, "IECMS Companion")
        self.assertContains(self.response, "general information only")

    def test_index_shows_privacy_reminder_and_disclaimer(self) -> None:
        self.assertContains(self.response, "Do not type names")
        self.assertContains(self.response, "not an official Judicial Service")

    def test_index_has_no_language_selector(self) -> None:
        self.assertNotContains(self.response, 'name="language"')
        self.assertNotContains(self.response, "Shona")

    def test_index_shows_fictional_example_questions(self) -> None:
        self.assertContains(self.response, "What is an affidavit?")
        self.assertContains(self.response, "fictional")

    def test_index_shows_iecms_procedure_examples(self) -> None:
        self.assertContains(self.response, "How do I file a case in IECMS?")
        self.assertContains(self.response, "How do I reset my password?")

    def test_index_defines_iecms_and_its_user_guide_scope(self) -> None:
        self.assertContains(
            self.response, "Integrated Electronic Case Management System"
        )
        self.assertContains(self.response, "Using IECMS")
        self.assertContains(self.response, "IECMS Getting Started Guide")
        self.assertContains(
            self.response,
            "cannot access your IECMS account",
        )

    def test_privacy_reminder_covers_login_credentials(self) -> None:
        self.assertContains(self.response, "passwords")
        self.assertContains(self.response, "IECMS login details")

    def test_index_has_accessible_chat_regions(self) -> None:
        self.assertContains(self.response, 'role="log"')
        self.assertContains(self.response, 'role="alert"')
        self.assertContains(self.response, 'role="status"')
        self.assertContains(self.response, 'aria-live="polite"')

    def test_index_does_not_expose_credentials(self) -> None:
        for marker in ("GEMINI_API_KEY", "api_key", "google-genai", "speechSynthesis"):
            self.assertNotContains(self.response, marker)


class ChatEndpointValidationTests(EmptyCorpusTestCase):
    """Transport and input validation for the chat endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("chatbot:chat")

    def post(self, payload, **kwargs):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            **kwargs,
        )

    def test_get_is_not_allowed(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_empty_question_is_rejected(self) -> None:
        response = self.post({"question": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_input")
        self.assertEqual(response.json()["error"]["field"], "question")

    def test_missing_question_is_rejected(self) -> None:
        response = self.post({})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["field"], "question")

    def test_overlong_question_is_rejected(self) -> None:
        overlong = "a" * (settings.CHAT_MAX_QUESTION_LENGTH + 1)
        response = self.post({"question": overlong})
        self.assertEqual(response.status_code, 400)

    def test_question_at_the_limit_is_accepted(self) -> None:
        at_limit = "a" * settings.CHAT_MAX_QUESTION_LENGTH
        self.assertEqual(self.post({"question": at_limit}).status_code, 200)

    def test_invalid_json_is_rejected(self) -> None:
        response = self.client.post(
            self.url, data="not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_json")

    def test_non_object_json_is_rejected(self) -> None:
        response = self.client.post(
            self.url, data=json.dumps([1, 2, 3]), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    def test_csrf_protection_is_enforced(self) -> None:
        strict = Client(enforce_csrf_checks=True)
        response = strict.post(
            self.url,
            data=json.dumps({"question": "What is an affidavit?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


class NoEvidenceBehaviourTests(EmptyCorpusTestCase):
    """With no knowledge base the assistant must refuse, not improvise."""

    def test_question_without_corpus_is_insufficient(self) -> None:
        response = self.client.post(
            reverse("chatbot:chat"),
            data=json.dumps({"question": "What is an affidavit?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "insufficient_evidence")
        self.assertEqual(body["sources"], [])
        self.assertIn("does not establish a reliable answer", body["answer"])


class MockModeTests(EmptyCorpusTestCase):
    """Development mock mode is opt-in and always labelled."""

    def test_mock_mode_is_off_by_default(self) -> None:
        self.assertFalse(settings.ENABLE_MOCK_CHAT)

    @override_settings(ENABLE_MOCK_CHAT=True)
    def test_mock_mode_returns_labelled_placeholder(self) -> None:
        body = self.client.post(
            reverse("chatbot:chat"),
            data=json.dumps({"question": "What is an affidavit?"}),
            content_type="application/json",
        ).json()
        self.assertEqual(body["status"], "mock")
        self.assertEqual(body["label"], "MOCK")
        self.assertEqual(body["sources"], [])


class PersistencePrivacyTests(EmptyCorpusTestCase):
    """No chat content is persisted, by design."""

    def test_chatbot_app_defines_no_models(self) -> None:
        config = apps.get_app_config("chatbot")
        self.assertEqual(list(config.get_models()), [])

    def test_legal_sources_app_defines_no_models(self) -> None:
        config = apps.get_app_config("legal_sources")
        self.assertEqual(list(config.get_models()), [])

    def test_question_is_not_written_to_the_session(self) -> None:
        self.client.post(
            reverse("chatbot:chat"),
            data=json.dumps({"question": "What is a subpoena?"}),
            content_type="application/json",
        )
        self.assertEqual(dict(self.client.session), {})


class SourceDocumentViewTests(EmptyCorpusTestCase):
    """Citations link to registered local documents through a safe view."""

    def setUp(self) -> None:
        super().setUp()
        self.paths = CorpusPaths.default(self.corpus_root)
        self.paths.ensure_directories()
        self.document = self.paths.documents_dir / "sample-source.pdf"
        self.document.write_bytes(b"%PDF-1.4\n% test fixture\n")
        self._register("tst-doc", self.document)

    def _register(self, source_id: str, path: Path) -> None:
        save_registry(
            self.paths.registry,
            {
                source_id: SourceMetadata(
                    source_id=source_id,
                    title="Test document",
                    issuing_authority="TEST FIXTURE",
                    verification_status="verified",
                    local_path=str(path),
                )
            },
        )

    def test_registered_document_is_served_inline(self) -> None:
        response = self.client.get(
            reverse("chatbot:source_document", args=["tst-doc"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(
            b"".join(response.streaming_content).startswith(b"%PDF")
        )

    def test_unknown_source_is_not_found(self) -> None:
        response = self.client.get(
            reverse("chatbot:source_document", args=["does-not-exist"])
        )
        self.assertEqual(response.status_code, 404)

    def test_file_outside_the_corpus_is_refused(self) -> None:
        outside = Path(tempfile.gettempdir()) / "iecms-outside-source.pdf"
        outside.write_bytes(b"not a corpus document")
        self.addCleanup(outside.unlink, missing_ok=True)
        self._register("tst-outside", outside)
        response = self.client.get(
            reverse("chatbot:source_document", args=["tst-outside"])
        )
        self.assertEqual(response.status_code, 404)


class DocumentSummaryTests(EmptyCorpusTestCase):
    """Uploaded documents are summarised, labelled and then deleted."""

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("chatbot:summarise")

    def _upload(
        self,
        name: str = "notes.txt",
        content: bytes = b"An affidavit is a sworn written statement.",
        content_type: str = "text/plain",
    ) -> SimpleUploadedFile:
        return SimpleUploadedFile(name, content, content_type=content_type)

    def test_summary_is_returned_and_labelled(self) -> None:
        result = SummaryResult(
            text="This document explains what an affidavit is.",
            model="test-model",
            provider="test",
            characters=42,
            truncated=False,
        )
        with mock.patch.object(
            summarisation_service, "is_available", return_value=True
        ), mock.patch.object(
            summarisation_service, "summarise_document", return_value=result
        ):
            response = self.client.post(self.url, {"document": self._upload()})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["label"], "DOCUMENT SUMMARY")
        self.assertIn("affidavit", body["summary"])
        self.assertIn("not an approved source", body["notice"])
        self.assertEqual(body["filename"], "notes.txt")

    def test_summary_without_a_provider_is_not_invented(self) -> None:
        with mock.patch.object(
            summarisation_service, "is_available", return_value=False
        ):
            response = self.client.post(self.url, {"document": self._upload()})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "summary_unavailable")
        self.assertIn("not configured", body["message"])
        self.assertNotIn("summary", body)

    def test_missing_file_is_rejected(self) -> None:
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "missing_file")

    def test_unsupported_file_type_is_rejected(self) -> None:
        response = self.client.post(
            self.url,
            {
                "document": self._upload(
                    "program.exe", b"MZ", "application/octet-stream"
                )
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_type")

    def test_oversized_file_is_rejected(self) -> None:
        with override_settings(DOCUMENT_SUMMARY_MAX_BYTES=10):
            response = self.client.post(
                self.url, {"document": self._upload(content=b"x" * 20)}
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "too_large")

    def test_file_without_readable_text_is_reported(self) -> None:
        response = self.client.post(
            self.url, {"document": self._upload(content=b"   \n\n  \n")}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_text")

    def test_uploaded_file_is_deleted_after_summarising(self) -> None:
        captured: dict[str, Path] = {}

        def fake_extract(source_id, path, *, ocr=False):
            captured["path"] = Path(path)
            self.assertTrue(Path(path).exists())
            return ExtractedDocument(
                source_id=source_id,
                blocks=[
                    ExtractedBlock(
                        block_id="b0",
                        text="An affidavit is a sworn written statement.",
                        order=0,
                    )
                ],
            )

        result = SummaryResult("A short summary.", "test-model", "test", 44, False)
        with mock.patch("chatbot.views.extract", side_effect=fake_extract), \
            mock.patch.object(
                summarisation_service, "is_available", return_value=True
            ), mock.patch.object(
                summarisation_service, "summarise_document", return_value=result
            ):
            response = self.client.post(self.url, {"document": self._upload()})

        self.assertEqual(response.status_code, 200)
        self.assertIn("path", captured)
        self.assertFalse(captured["path"].exists())


class FrontendAssetTests(TestCase):
    """Guards on the static assets, which have no JavaScript test runner."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        static = settings.BASE_DIR / "chatbot" / "static" / "chatbot"
        cls.js = (static / "js" / "chat.js").read_text(encoding="utf-8")
        cls.css = (static / "css" / "styles.css").read_text(encoding="utf-8")

    def test_answer_text_is_never_inserted_as_html(self) -> None:
        self.assertIn("textContent", self.js)
        self.assertNotIn("innerHTML", self.js)

    def test_speech_support_is_detected_before_use(self) -> None:
        self.assertIn("speechSynthesis", self.js)
        self.assertIn("SpeechSynthesisUtterance", self.js)
        self.assertIn("unsupported", self.js)

    def test_synthetic_warning_is_displayed(self) -> None:
        self.assertIn("synthetic_warning", self.js)
        self.assertIn("msg__warning", self.js)

    def test_documented_steps_are_rendered_as_a_list(self) -> None:
        self.assertIn("msg__steps", self.js)
        self.assertIn("msg__steps", self.css)
        self.assertIn("opts.steps", self.js)

    def test_follow_up_context_contains_questions_only(self) -> None:
        self.assertIn("historyEntry", self.js)
        self.assertIn("currentHistory()", self.js)
        # Only question text and intent are sent; answers are not re-uploaded.
        self.assertIn("intent:", self.js)
        self.assertIn("question: entry.question", self.js)

    def test_sources_are_opened_safely(self) -> None:
        self.assertIn("noopener", self.js)
        self.assertIn("noreferrer", self.js)

    def test_source_titles_are_clickable(self) -> None:
        self.assertIn("source.local_url", self.js)
        self.assertIn("#page=", self.js)
        self.assertIn("source__title--link", self.js)
        self.assertIn("source__title--link", self.css)

    def test_conversation_is_memory_only(self) -> None:
        """The visible conversation must not be written to browser storage."""
        self.assertNotIn("window.sessionStorage", self.js)
        self.assertNotIn("document.cookie", self.js)
        # localStorage is used for the theme only, never for messages.
        self.assertNotIn("localStorage.setItem(\"iecms", self.js)
