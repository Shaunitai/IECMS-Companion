"""Tests for the general-knowledge fallback and the verifier agents.

The agents use the same provider as the answer generator. These tests mock the
provider calls, so no network access or credentials are required.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import SimpleTestCase, override_settings

from chatbot.services import (
    explanation_service,
    general_knowledge_service,
    llm_service,
    verification_service,
)

from .corpus_fixtures import EmptyCorpusTestCase


def _agent_reply(verdict: str, revised: str = "") -> llm_service.LLMAnswer:
    payload = json.dumps(
        {
            "verdict": verdict,
            "issues": [] if verdict == "approve" else ["Needs attention."],
            "revised_answer": revised,
        }
    )
    return llm_service.LLMAnswer(payload, "test-model", "test-provider", 1)


@override_settings(
    LLM_API_KEY="test-key",
    ENABLE_ANSWER_VERIFICATION=True,
    VERIFICATION_MAX_ROUNDS=1,
)
class VerificationPanelTests(SimpleTestCase):
    def test_three_agents_can_approve_a_draft(self) -> None:
        replies = [_agent_reply("approve") for _ in range(3)]
        with mock.patch.object(
            llm_service, "generate_text", side_effect=replies
        ) as generate:
            result = verification_service.verify_answer(
                question="What is a subpoena?",
                draft="A subpoena is a court order requiring attendance.",
            )
        self.assertTrue(result.approved)
        self.assertEqual(result.rounds, 1)
        self.assertEqual(len(result.verdicts), 3)
        self.assertEqual(generate.call_count, 3)

    def test_a_revision_is_reverified_and_used(self) -> None:
        replies = [
            _agent_reply("approve"),
            _agent_reply("revise", "A subpoena requires a witness to attend."),
            _agent_reply("approve"),
            _agent_reply("approve"),
            _agent_reply("approve"),
            _agent_reply("approve"),
        ]
        with mock.patch.object(llm_service, "generate_text", side_effect=replies):
            result = verification_service.verify_answer(
                question="What is a subpoena?", draft="Unsupported draft."
            )
        self.assertTrue(result.approved)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(
            result.text, "A subpoena requires a witness to attend."
        )

    def test_a_rejection_blocks_the_draft(self) -> None:
        replies = [
            _agent_reply("approve"),
            _agent_reply("reject"),
            _agent_reply("approve"),
        ]
        with mock.patch.object(llm_service, "generate_text", side_effect=replies):
            result = verification_service.verify_answer(
                question="What is a subpoena?", draft="Unsafe draft."
            )
        self.assertFalse(result.approved)
        self.assertIn("rejected", result.reason)

    def test_unusable_agent_reply_is_unavailable(self) -> None:
        with mock.patch.object(
            llm_service,
            "generate_text",
            return_value=llm_service.LLMAnswer("not json", "m", "p", 1),
        ):
            with self.assertRaises(verification_service.VerificationUnavailable):
                verification_service.verify_answer(
                    question="What is a subpoena?", draft="A draft."
                )

    def test_provider_failure_is_unavailable(self) -> None:
        with mock.patch.object(
            llm_service,
            "generate_text",
            side_effect=llm_service.LLMServiceError("down", code="unavailable"),
        ):
            with self.assertRaises(verification_service.VerificationUnavailable):
                verification_service.verify_answer(
                    question="What is a subpoena?", draft="A draft."
                )


@override_settings(
    ENABLE_WEB_FALLBACK=False,
    ENABLE_GENERAL_KNOWLEDGE_FALLBACK=True,
    ENABLE_ANSWER_VERIFICATION=True,
)
class GeneralKnowledgeFallbackTests(EmptyCorpusTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.draft = llm_service.LLMAnswer(
            "A subpoena is a court order requiring a witness to attend. "
            "I am not certain of the current procedure, so confirm it with "
            "the court.",
            "test-model",
            "test-provider",
            1,
        )

    def _approved(self, text: str) -> verification_service.VerificationResult:
        return verification_service.VerificationResult(
            approved=True,
            text=text,
            verdicts=(
                verification_service.AgentVerdict("grounding", "approve"),
                verification_service.AgentVerdict("safety", "approve"),
                verification_service.AgentVerdict("clarity", "approve"),
            ),
            rounds=1,
        )

    def test_general_knowledge_answer_is_labelled_and_verified(self) -> None:
        with override_settings(LLM_API_KEY="test-key"), mock.patch.object(
            general_knowledge_service, "draft_answer", return_value=self.draft
        ), mock.patch.object(
            verification_service,
            "verify_answer",
            return_value=self._approved(self.draft.text),
        ) as verify:
            result = explanation_service.answer_question(
                "What is a subpoena?", "en"
            )

        verify.assert_called_once()
        self.assertEqual(
            result["status"], explanation_service.STATUS_GENERAL_KNOWLEDGE
        )
        self.assertEqual(result["label"], "GENERAL KNOWLEDGE (UNVERIFIED)")
        self.assertIn("general knowledge", result["notice"])
        self.assertIn("unverified", result["answer"].lower())
        self.assertEqual(result["sources"], [])
        self.assertEqual(
            [agent["name"] for agent in result["verification"]["agents"]],
            ["grounding", "safety", "clarity"],
        )

    def test_rejected_draft_is_not_shown(self) -> None:
        rejected = verification_service.VerificationResult(
            approved=False, text=self.draft.text, reason="Unsupported."
        )
        with override_settings(LLM_API_KEY="test-key"), mock.patch.object(
            general_knowledge_service, "draft_answer", return_value=self.draft
        ), mock.patch.object(
            verification_service, "verify_answer", return_value=rejected
        ):
            result = explanation_service.answer_question(
                "What is a subpoena?", "en"
            )

        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
        self.assertEqual(result["sources"], [])
        self.assertNotIn(
            "A subpoena is a court order", result["answer"]
        )

    def test_verification_failure_does_not_leak_the_draft(self) -> None:
        with override_settings(LLM_API_KEY="test-key"), mock.patch.object(
            general_knowledge_service, "draft_answer", return_value=self.draft
        ), mock.patch.object(
            verification_service,
            "verify_answer",
            side_effect=verification_service.VerificationUnavailable("down"),
        ):
            result = explanation_service.answer_question(
                "What is a subpoena?", "en"
            )

        self.assertEqual(result["status"], explanation_service.STATUS_PROVIDER_ERROR)
        self.assertNotIn("A subpoena is a court order", result["answer"])
        self.assertIn("could not be verified", result["answer"])

    def test_without_a_model_the_assistant_refuses(self) -> None:
        with override_settings(LLM_API_KEY=""), mock.patch.object(
            general_knowledge_service, "draft_answer"
        ) as draft:
            result = explanation_service.answer_question(
                "What is a subpoena?", "en"
            )
        draft.assert_not_called()
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
