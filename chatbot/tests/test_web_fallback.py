"""Tests for the open-web fallback.

The web fallback deliberately relaxes the project's "no approved evidence, no
answer" rule, so these tests check two things above all: that it **never** fires
unless explicitly enabled, and that everything it returns is labelled
unverified with the exact URL it came from.

No test performs a real network request: ``urllib.request.urlopen`` is patched
either by the test runner (which blocks it) or by the fake below.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, override_settings

from chatbot.services import (
    explanation_service,
    llm_service,
    verification_service,
    web_search_service,
)

from .corpus_fixtures import EmptyCorpusTestCase


class FakeResponse:
    """Minimal stand-in for an ``urlopen`` response."""

    def __init__(self, body: bytes, content_type: str = "text/html") -> None:
        self._body = body
        self.status = 200
        self.headers = {"Content-Type": content_type}

    def read(self, *args: object) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def web_outcome(
    *pages: tuple[str, str],
    passages: tuple[str, ...] = ("A subpoena requires a witness to attend.",),
    query: str = "What is a subpoena?",
) -> web_search_service.WebSearchOutcome:
    """Build a WebSearchOutcome from ``(title, url)`` pairs."""
    sources = tuple(
        web_search_service.WebSource(
            title=title,
            url=url,
            host=url.split("/")[2],
            snippet="snippet",
            passages=passages,
            retrieved_on="2026-10-14",
        )
        for title, url in pages
    )
    return web_search_service.WebSearchOutcome(query=query, sources=sources)


class SearchParsingTests(SimpleTestCase):
    """Parsing and URL decoding, with no network access."""

    CANNED = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg='
        'https%3A%2F%2Fveritaszim.net%2Fnode%2F5166&amp;rut=abc">High Court Rules</a>'
        '<a class="result__snippet">Service of <b>process</b> explained</a>'
        '<a class="result__a" href="https://www.jsc.org.zw/rules.pdf">JSC rules</a>'
        '<a class="result__snippet">Judicial Service Commission</a>'
    )

    def test_redirect_url_is_unwrapped_and_entities_cleaned(self) -> None:
        with mock.patch.object(web_search_service, "_robots_allow", return_value=(True, "ok")), \
             mock.patch.object(web_search_service.urllib.request, "urlopen",
                               return_value=FakeResponse(self.CANNED.encode())):
            results, problem = web_search_service._search_duckduckgo("q", 5)

        self.assertEqual(problem, "")
        self.assertEqual(results[0][1], "https://veritaszim.net/node/5166")
        self.assertEqual(results[0][0], "High Court Rules")
        self.assertEqual(results[0][2], "Service of process explained")
        self.assertEqual(results[1][1], "https://www.jsc.org.zw/rules.pdf")

    def test_search_refuses_when_robots_disallows(self) -> None:
        with mock.patch.object(
            web_search_service, "_robots_allow", return_value=(False, "disallowed here")
        ):
            results, problem = web_search_service._search_duckduckgo("q", 5)
        self.assertEqual(results, [])
        self.assertIn("disallowed", problem)

    def test_fetch_refuses_when_robots_disallows(self) -> None:
        with mock.patch.object(
            web_search_service, "_robots_allow", return_value=(False, "disallowed here")
        ):
            passages, error = web_search_service._fetch_passages("https://x.test/a", "web-1")
        self.assertEqual(passages, [])
        self.assertIn("disallowed", error)

    def test_unsupported_content_type_is_reported(self) -> None:
        with mock.patch.object(web_search_service, "_robots_allow", return_value=(True, "ok")), \
             mock.patch.object(web_search_service.urllib.request, "urlopen",
                               return_value=FakeResponse(b"binary", "application/zip")):
            passages, error = web_search_service._fetch_passages("https://x.test/a", "web-1")
        self.assertEqual(passages, [])
        self.assertIn("Unsupported content type", error)


class WebPassageTests(SimpleTestCase):
    def test_web_passages_are_marked_unverified_with_their_url(self) -> None:
        passages = web_search_service.build_web_passages(
            web_outcome(("High Court Rules", "https://veritaszim.net/node/5166"))
        )
        self.assertEqual(len(passages), 1)
        passage = passages[0]
        self.assertEqual(passage.source_url, "https://veritaszim.net/node/5166")
        self.assertEqual(passage.verification_status, web_search_service.WEB_STATUS)
        self.assertEqual(passage.tier_label, web_search_service.WEB_TIER_LABEL)
        self.assertIn("UNVERIFIED WEB SOURCE", passage.disclosure)
        self.assertIn("2026-10-14", passage.disclosure)

    def test_pages_that_could_not_be_read_are_not_usable(self) -> None:
        outcome = web_search_service.WebSearchOutcome(
            query="q",
            sources=(
                web_search_service.WebSource(
                    title="t", url="https://x.test/a", host="x.test", error="HTTP 404."
                ),
            ),
        )
        self.assertFalse(outcome.has_evidence)
        self.assertEqual(web_search_service.build_web_passages(outcome), [])

    def test_off_topic_page_is_dropped(self) -> None:
        outcome = web_outcome(
            ("Companies", "https://x.test/a"),
            passages=(
                "The capital redemption reserve may be reduced by a special "
                "resolution of the company.",
            ),
        )
        self.assertEqual(web_search_service.build_web_passages(outcome), [])

    def test_only_relevant_sentences_are_kept(self) -> None:
        outcome = web_outcome(
            ("Court guide", "https://x.test/a"),
            passages=(
                "This page explains the history of the courthouse. "
                "A subpoena requires a witness to attend court. "
                "Parking is available behind the building.",
            ),
        )
        passages = web_search_service.build_web_passages(outcome)
        self.assertEqual(len(passages), 1)
        self.assertIn("subpoena", passages[0].text.lower())
        self.assertNotIn("parking", passages[0].text.lower())
        self.assertNotIn("history", passages[0].text.lower())


class WebFallbackDisabledTests(EmptyCorpusTestCase):
    """With the fallback off (the default), behaviour is unchanged."""

    def test_search_is_not_performed_when_disabled(self) -> None:
        with mock.patch.object(
            web_search_service, "search_and_fetch"
        ) as patched:
            result = explanation_service.answer_question("What is a subpoena?", "en")
        patched.assert_not_called()
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
        self.assertEqual(result["sources"], [])
        self.assertFalse(result["web_fallback"])


@override_settings(ENABLE_WEB_FALLBACK=True)
class WebFallbackEnabledTests(EmptyCorpusTestCase):
    """The fallback only runs after approved retrieval has already failed."""

    def answer(self, text: str = "A subpoena requires a witness to attend [S1].", **kwargs):
        generated = llm_service.LLMAnswer(text, "test-model", "google", 1)
        approved = verification_service.VerificationResult(
            approved=True, text=text, rounds=1
        )
        with mock.patch.object(
            web_search_service, "search_and_fetch",
            return_value=web_outcome(("High Court Rules", "https://veritaszim.net/node/5166")),
        ) as search, mock.patch.object(
            llm_service, "generate_explanation", return_value=generated
        ), mock.patch.object(
            verification_service, "verify_answer", return_value=approved
        ):
            with mock.patch.object(llm_service, "is_configured", return_value=True):
                result = explanation_service.answer_question(
                    kwargs.get("question", "What is a subpoena?")
                )
        return result, search

    def test_answer_cites_the_web_page_and_discloses_it(self) -> None:
        result, search = self.answer()
        search.assert_called_once()
        self.assertEqual(result["status"], explanation_service.STATUS_WEB_FALLBACK)
        self.assertEqual(result["label"], "WEB SOURCE (UNVERIFIED)")
        self.assertTrue(result["web_fallback"])
        self.assertIn("open-web search", result["notice"])
        self.assertIn("NOT approved official sources", result["notice"])
        self.assertIn("unverified web pages", result["answer"])

        source = result["sources"][0]
        self.assertEqual(source["source_url"], "https://veritaszim.net/node/5166")
        self.assertEqual(source["verification_status"], "web_unverified")
        self.assertFalse(source["synthetic"])
        self.assertIn("UNVERIFIED WEB SOURCE", source["disclosure"])

    def test_no_usable_pages_falls_back_to_an_honest_refusal(self) -> None:
        empty = web_search_service.WebSearchOutcome(
            query="q", diagnostics=("The search returned no usable results.",)
        )
        with mock.patch.object(web_search_service, "search_and_fetch", return_value=empty):
            result = explanation_service.answer_question("What is a subpoena?", "en")
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
        self.assertFalse(result["web_fallback"])
        self.assertIn("no usable results", " ".join(result["diagnostics"]))

    def test_web_answer_without_a_citation_is_not_shown(self) -> None:
        result, _ = self.answer("A subpoena is a kind of court order.")
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)

    def test_off_topic_web_results_are_not_used(self) -> None:
        off_topic = web_outcome(
            ("Companies", "https://x.test/a"),
            passages=("The capital redemption reserve may be reduced.",),
        )
        with mock.patch.object(
            web_search_service, "search_and_fetch", return_value=off_topic
        ):
            result = explanation_service.answer_question(
                "What is a subpoena?", "en"
            )
        self.assertEqual(result["status"], explanation_service.STATUS_INSUFFICIENT)
        self.assertEqual(result["sources"], [])

    def test_an_unconfirmed_web_claim_is_not_presented_as_approved(self) -> None:
        """A web answer must never be labelled as a verified/approved source."""
        result, _ = self.answer()
        labels = {s["verification_status"] for s in result["sources"]}
        self.assertEqual(labels, {"web_unverified"})
        self.assertNotEqual(result["label"], "")


class WebAnswerProviderTests(SimpleTestCase):
    """Answer generation still uses the configured main provider."""

    def test_answer_generation_uses_the_main_provider(self) -> None:
        with override_settings(
            LLM_PROVIDER="google",
            LLM_MODEL="gemma-4-31b-it",
            LLM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai",
            LLM_API_KEY="gemini-key",
        ):
            config = llm_service.get_llm_config()
        self.assertEqual(config.provider, "google")
        self.assertEqual(config.model, "gemma-4-31b-it")
        self.assertEqual(config.api_key, "gemini-key")
