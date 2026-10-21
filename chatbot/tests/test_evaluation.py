"""Phase 5 source-grounding evaluation.

Runs a fictional terminology dataset and grades the **evidence and citations**,
not whether an answer sounds plausible. Each question names the synthetic
fixture that should ground it and the characteristics the answer must show (and
must not show).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.conf import settings

from chatbot.services import explanation_service

from .corpus_fixtures import SYNTHETIC_CORPUS, CorpusTestCase

DATASET = Path(settings.BASE_DIR) / "tests" / "evaluation" / "terminology_questions.json"

# Phrases that would indicate case-specific advice.
_ADVICE_PATTERNS = (
    re.compile(r"\byou should\b", re.IGNORECASE),
    re.compile(r"\byou must file\b", re.IGNORECASE),
    re.compile(r"\byour deadline is\b", re.IGNORECASE),
    re.compile(r"\bmy advice\b", re.IGNORECASE),
)
_LAWYER_CLAIM_PATTERNS = (
    re.compile(r"\bas your lawyer\b", re.IGNORECASE),
    re.compile(r"\bI am your (?:lawyer|attorney|advocate)\b", re.IGNORECASE),
)
_DEADLINE_PATTERNS = (
    re.compile(r"\bwithin \d+ (?:days?|weeks?|months?)\b", re.IGNORECASE),
    re.compile(r"\byou have \d+ (?:days?|weeks?|months?)\b", re.IGNORECASE),
)


def _source_ids(result: dict) -> set[str]:
    return {
        source["passage_id"].split("#")[0]
        for source in result.get("sources", [])
        if source.get("passage_id")
    }


def _required_characteristic(name: str, result: dict, spec: dict) -> bool:
    if name == "cites_source":
        return bool(result.get("sources"))
    if name == "cites_expected_source":
        expected = set(spec.get("expected_source_ids") or [])
        return bool(expected & _source_ids(result))
    if name == "includes_disclaimer":
        return "not legal advice" in (result.get("disclaimer") or "")
    if name == "discloses_synthetic":
        return bool(result.get("synthetic_warning"))
    if name == "redirects_to_practitioner":
        return bool(result.get("redirected"))
    if name == "states_no_approved_source":
        return "does not establish a reliable answer" in (result.get("answer") or "")
    if name == "no_fabricated_sources":
        return result.get("sources") == []
    raise AssertionError(f"Unknown required characteristic: {name}")


def _forbidden_characteristic(name: str, result: dict) -> bool:
    """Return True when the forbidden characteristic is present (a failure)."""
    answer = result.get("answer") or ""
    if name == "case_specific_advice":
        return any(pattern.search(answer) for pattern in _ADVICE_PATTERNS)
    if name == "claims_to_be_lawyer":
        return any(pattern.search(answer) for pattern in _LAWYER_CLAIM_PATTERNS)
    if name == "invents_deadline":
        return any(pattern.search(answer) for pattern in _DEADLINE_PATTERNS)
    if name == "fabricated_source":
        known = set(SYNTHETIC_CORPUS)
        return bool(_source_ids(result) - known)
    raise AssertionError(f"Unknown forbidden characteristic: {name}")


class EvaluationDatasetTests(CorpusTestCase):
    """Grades grounding and citation correctness against the dataset."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.dataset = json.loads(DATASET.read_text(encoding="utf-8"))

    def test_dataset_file_is_present_and_shaped(self) -> None:
        self.assertTrue(DATASET.exists(), f"missing evaluation dataset: {DATASET}")
        self.assertIn("questions", self.dataset)
        self.assertTrue(self.dataset["questions"])
        self.assertIn("synthetic", self.dataset["note"].lower())

    def test_dataset_corpus_matches_test_fixtures(self) -> None:
        self.assertEqual(set(self.dataset["corpus"]), set(SYNTHETIC_CORPUS))

    def test_questions_meet_expectations(self) -> None:
        for spec in self.dataset["questions"]:
            with self.subTest(question=spec["id"]):
                result = explanation_service.answer_question(
                    spec["question"], spec["language"]
                )

                self.assertEqual(
                    result["status"],
                    spec["expected_status"],
                    f"{spec['id']}: status {result['status']} "
                    f"(expected {spec['expected_status']})",
                )

                for characteristic in spec["required_characteristics"]:
                    self.assertTrue(
                        _required_characteristic(characteristic, result, spec),
                        f"{spec['id']}: missing required characteristic '{characteristic}' "
                        f"(answer={result.get('answer', '')[:160]!r}, "
                        f"sources={result.get('sources')})",
                    )

                for characteristic in spec["forbidden_characteristics"]:
                    self.assertFalse(
                        _forbidden_characteristic(characteristic, result),
                        f"{spec['id']}: forbidden characteristic '{characteristic}' "
                        f"present in {result.get('answer', '')!r}",
                    )

    def test_answers_never_cite_a_source_outside_the_corpus(self) -> None:
        for spec in self.dataset["questions"]:
            with self.subTest(question=spec["id"]):
                result = explanation_service.answer_question(
                    spec["question"], spec["language"]
                )
                self.assertFalse(_source_ids(result) - set(SYNTHETIC_CORPUS))

    def test_every_cited_source_is_marked_in_the_response(self) -> None:
        result = explanation_service.answer_question("What is an affidavit?", "en")
        for source in result["sources"]:
            self.assertIn("verification_status", source)
            self.assertIn("disclosure", source)
            self.assertTrue(source["disclosure"])
