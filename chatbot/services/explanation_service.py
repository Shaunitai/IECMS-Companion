"""Turn a user question into a grounded, plain-language explanation.

Pipeline (see docs/ARCHITECTURE.md):

1. Reject input that appears to contain personal identifiers (never forwarded).
2. Screen for requests that cross the legal-advice boundary.
3. Retrieve evidence from the approved knowledge base.
4. If there is not enough authoritative evidence, say so — do not guess.
5. Ask the configured LLM to explain the evidence in plain language, or, when
   no provider is configured, quote the evidence verbatim in extractive mode.
6. Validate every citation marker against the evidence that was actually
   supplied, and drop anything the model invented.

The service never fabricates a legal statement and never returns an answer that
was not either generated from supplied evidence or quoted directly from it.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from django.conf import settings

from legal_sources.indexing import query_terms, term_in_text

from ..constants import (
    DEFAULT_LANGUAGE,
    DISCLAIMER_TEXT,
    DOCUMENT_SUMMARY_GUIDANCE_TEXT,
    GENERAL_KNOWLEDGE_CAVEAT,
    GENERAL_KNOWLEDGE_LABEL,
    GENERAL_KNOWLEDGE_NOTICE,
    IECMS_MANUAL_CAVEAT,
    IECMS_MANUAL_LABEL,
    IECMS_MANUAL_LOW_CONFIDENCE_CAVEAT,
    IECMS_MANUAL_NOT_COVERED_TEXT,
    INSUFFICIENT_EVIDENCE_TEXT,
    LOW_CONFIDENCE_CAVEAT,
    MAX_HISTORY_ENTRIES,
    MAX_HISTORY_QUESTION_CHARS,
    MOCK_MODE_NOTICE,
    SYNTHETIC_WARNING,
    VERIFICATION_FAILED_TEXT,
    WEB_FALLBACK_CAVEAT,
    WEB_FALLBACK_NOTICE,
)
from . import (
    citation_service,
    general_knowledge_service,
    intent_service,
    llm_service,
    privacy_service,
    procedure_service,
    retrieval_service,
    safety_service,
    verification_service,
    web_search_service,
)
from .intent_service import IntentResult
from .retrieval_service import RetrievedPassage, RetrievalResult

logger = logging.getLogger(__name__)

# Machine-readable statuses. The frontend uses these to pick a presentation.
STATUS_OK = "ok"
STATUS_MOCK = "mock"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_REFUSED = "refused"
STATUS_REJECTED_PRIVACY = "rejected_privacy"
STATUS_PROVIDER_ERROR = "provider_error"
STATUS_EXTRACTIVE = "extractive"
# Answer built from an open-web search because no approved source covered it.
STATUS_WEB_FALLBACK = "web_fallback"
# Answer built from the model's general knowledge, verified but not sourced.
STATUS_GENERAL_KNOWLEDGE = "general_knowledge"

# The model is instructed to reply with exactly this marker when the supplied
# sources do not answer the question. Detecting an explicit marker is far more
# reliable than guessing from the wording of a normal answer.
NO_ANSWER_SENTINEL = "NO_APPROVED_ANSWER"

_LABELS = {
    STATUS_OK: "",
    STATUS_MOCK: "MOCK",
    STATUS_INSUFFICIENT: "NO APPROVED SOURCE",
    STATUS_REFUSED: "OUT OF SCOPE",
    STATUS_REJECTED_PRIVACY: "PERSONAL DATA",
    STATUS_PROVIDER_ERROR: "SERVICE UNAVAILABLE",
    STATUS_EXTRACTIVE: "DIRECT QUOTATION",
    STATUS_WEB_FALLBACK: "WEB SOURCE (UNVERIFIED)",
    STATUS_GENERAL_KNOWLEDGE: GENERAL_KNOWLEDGE_LABEL,
}

# Kept for Phase 1 development mode.
MOCK_ANSWER_TEXT = (
    "[MOCK RESPONSE] The chat interface is working, but the explanation "
    "engine is not connected yet. This placeholder is returned instead of a "
    "real answer: no language model was contacted and no approved legal "
    "source was searched. Do not rely on this text."
)


def _payload(
    *,
    status: str,
    question: str,
    language: str,
    answer: str,
    sources: list[dict] | None = None,
    notice: str = "",
    extra: dict | None = None,
) -> dict:
    payload = {
        "status": status,
        "label": _LABELS.get(status, ""),
        "question": question,
        "language": language,
        "answer": answer,
        "sources": sources or [],
        "notice": notice,
        "disclaimer": DISCLAIMER_TEXT,
        "model": "",
        "redirected": False,
        "synthetic_warning": "",
        "confidence": "none",
        "confidence_caveat": "",
        # Routing and presentation metadata. They are additive so existing
        # clients that only read answer/sources keep working.
        "intent": "",
        "is_follow_up": False,
        "manual": False,
        "steps": [],
    }
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Development mode
# ---------------------------------------------------------------------------


def is_mock_mode() -> bool:
    """Whether the app is currently serving labelled mock answers."""
    return bool(settings.ENABLE_MOCK_CHAT)


def build_mock_response(question: str) -> dict:
    """Return a clearly-labelled placeholder payload (development mode only)."""
    return _payload(
        status=STATUS_MOCK,
        question=question,
        language=DEFAULT_LANGUAGE,
        answer=MOCK_ANSWER_TEXT,
        notice=MOCK_MODE_NOTICE,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _synthetic_warning(passages: tuple[RetrievedPassage, ...]) -> str:
    if any(passage.is_synthetic for passage in passages):
        return SYNTHETIC_WARNING
    return ""


def _verification_evidence(labelled: list[tuple[str, RetrievedPassage]]) -> str:
    """Render the evidence shown to the reviewer agents."""
    lines: list[str] = []
    for label, passage in labelled:
        header = f"[{label}] {passage.document_title} \u2014 {passage.issuing_authority}"
        if passage.source_url:
            header += f" ({passage.source_url})"
        lines.extend([header, passage.text, ""])
    evidence = "\n".join(lines)
    limit = int(getattr(settings, "VERIFICATION_MAX_EVIDENCE_CHARS", 8000))
    return evidence[:limit]


def _verification_meta(result: verification_service.VerificationResult) -> dict:
    """Public, reasoning-free description of the reviewer panel's decision."""
    return {
        "rounds": result.rounds,
        "agents": [
            {"name": verdict.agent, "verdict": verdict.verdict}
            for verdict in result.verdicts
        ],
    }


def _general_knowledge_payload(
    *,
    question: str,
    language: str,
    intent: IntentResult,
    redirect_note: str,
) -> dict | None:
    """Answer from general knowledge when the model is available.

    Returns ``None`` when the feature is disabled or no model is configured,
    so the caller can fall back to the honest refusal. A drafted answer is
    never shown until the verification panel approves it.
    """
    if not settings.ENABLE_GENERAL_KNOWLEDGE_FALLBACK:
        return None
    if not general_knowledge_service.is_available():
        return None

    try:
        draft = general_knowledge_service.draft_answer(
            question, context=intent.context_note
        )
    except llm_service.LLMServiceError as exc:
        logger.warning("General-knowledge draft failed: code=%s", exc.code)
        return None

    if not (draft.text or "").strip():
        return None

    try:
        review = verification_service.verify_answer(
            question=question,
            draft=draft.text,
            evidence="",
            source_kind="general knowledge",
        )
    except verification_service.VerificationUnavailable:
        logger.warning("General-knowledge answer could not be verified.")
        return _payload(
            status=STATUS_PROVIDER_ERROR,
            question=question,
            language=language,
            answer=VERIFICATION_FAILED_TEXT,
            extra={
                "error_code": "verification_failed",
                "intent": intent.intent,
                "is_follow_up": intent.is_follow_up,
            },
        )

    if not review.approved:
        logger.warning("General-knowledge answer was rejected by the panel.")
        return _payload(
            status=STATUS_INSUFFICIENT,
            question=question,
            language=language,
            answer=INSUFFICIENT_EVIDENCE_TEXT,
            extra={
                "diagnostics": [
                    "A draft answer was produced but the verification panel "
                    "did not approve it, so it was not shown."
                ],
                "intent": intent.intent,
                "is_follow_up": intent.is_follow_up,
            },
        )

    answer_text = review.text
    if redirect_note:
        answer_text = f"{answer_text}\n\n{redirect_note}"
    answer_text = f"{answer_text}\n\n{GENERAL_KNOWLEDGE_CAVEAT}"
    return _payload(
        status=STATUS_GENERAL_KNOWLEDGE,
        question=question,
        language=language,
        answer=answer_text,
        notice=GENERAL_KNOWLEDGE_NOTICE,
        extra={
            "model": draft.model,
            "provider": draft.provider,
            "confidence": "general",
            "verification": _verification_meta(review),
            "web_fallback": False,
            "intent": intent.intent,
            "is_follow_up": intent.is_follow_up,
        },
    )


# Text taken from a PDF text layer keeps the layout of the printed page: lines
# end where the page ended, not where the sentence ended. Joining those lines
# is what turns a document quotation into a readable paragraph.
_HYPHEN_LINE_BREAK = re.compile(r"(\w)-[ \t]*\n\s*(\w)")

# PDF font maps sometimes emit unassigned or private-use code points for a
# glyph they cannot resolve. Such a character renders as a black diamond in the
# interface and carries no meaning, so it is removed rather than displayed.
_UNREADABLE_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Co", "Cs"})


def _strip_unreadable(text: str) -> str:
    return "".join(
        char
        for char in text
        if char.isspace()
        or unicodedata.category(char) not in _UNREADABLE_CATEGORIES
    )


def _as_paragraph(text: str) -> str:
    """Collapse hard-wrapped source text into a single readable paragraph.

    Extracted documents (especially PDFs) store every printed line as a line
    break. Those breaks are page furniture, not sentence boundaries, so the
    answer would otherwise be displayed as a stack of fragments. Hyphenated
    words split across a line break are rejoined (``condo-\nation`` becomes
    ``condonation``) before remaining whitespace is collapsed.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\ufffd", " ")
    text = _HYPHEN_LINE_BREAK.sub(r"\1\2", text)
    text = _strip_unreadable(text)
    return " ".join(text.split())


# Upper bound on the extractive answer text so a reply stays short and readable
# even when the stored passage is a long chunk.
MAX_ANSWER_CHARS = 600

def _extractive_answer(passages: list[RetrievedPassage]) -> str:
    """Build a short, plain answer from the most relevant passages.

    A few chunks from the same source are joined into one paragraph and capped
    at ``MAX_ANSWER_CHARS`` so the reply reads like a normal answer rather than
    a labelled dump.
    """
    parts: list[str] = []
    budget = MAX_ANSWER_CHARS
    for passage in passages:
        piece = _as_paragraph(passage.text)
        if not piece:
            continue
        if budget <= 0:
            break
        if len(piece) > budget:
            piece = piece[:budget].rsplit(" ", 1)[0].strip()
        parts.append(piece)
        budget -= len(piece) + 1

    return " ".join(parts).strip()


def _clean_history(history) -> list[dict]:
    """Validate the bounded browser-supplied conversation context.

    Only previous question text and an optional intent are accepted. Entries
    containing personal identifiers or prompt-injection attempts are dropped
    rather than forwarded to retrieval or a provider.
    """
    if not history:
        return []
    entries: list[dict] = []
    if not isinstance(history, (list, tuple)):
        return entries
    for entry in list(history)[-MAX_HISTORY_ENTRIES:]:
        if isinstance(entry, dict):
            question = entry.get("question", "")
            intent = entry.get("intent", "")
        else:
            question = getattr(entry, "question", "")
            intent = getattr(entry, "intent", "")
        if not isinstance(question, str):
            continue
        question = " ".join(question.split())[:MAX_HISTORY_QUESTION_CHARS]
        if len(question) < 2:
            continue
        if privacy_service.scan(question).detected:
            logger.warning("Dropped conversation history containing personal data.")
            continue
        if safety_service.evaluate_question(question).blocks:
            logger.warning("Dropped conversation history containing a blocked request.")
            continue
        entries.append(
            {
                "question": question,
                "intent": str(intent)[:40] if intent else "",
            }
        )
    return entries


def _manual_extractive_answer(
    passages: list[RetrievedPassage],
) -> tuple[str, list[RetrievedPassage]]:
    """Quote the most relevant manual passages, preserving their order.

    Procedures often span more than one retrieved chunk, so a manual answer
    uses up to three passages rather than the single passage used for a
    definition. The text is still a quotation, not a generated explanation.
    """
    parts: list[str] = []
    used: list[RetrievedPassage] = []
    budget = 4000
    for passage in passages[:12]:
        piece = _as_paragraph(passage.text)
        if not piece or budget <= 0:
            continue
        if len(piece) > budget:
            piece = piece[:budget].rsplit(" ", 1)[0].strip()
        if not piece:
            continue
        parts.append(piece)
        used.append(passage)
        budget -= len(piece) + 1

    # One connected paragraph rather than one block per retrieved chunk.
    joined = " ".join(parts).strip()
    steps = procedure_service.extract_steps_from_passages(passages)
    if steps:
        text = (
            "The manual documents these steps:\n\n"
            + "\n".join(
                f"{index}. {step}" for index, step in enumerate(steps, start=1)
            )
        )
    else:
        text = joined

    return text, used


_PROPER_NOUN = re.compile(r"\b[A-Z][a-z]{2,}\b")

# Capitalised words that usually start a sentence rather than name something.
_SENTENCE_STARTERS = frozenset(
    """
    What How Where When Why Which Who Whom Can Could Should Would Will Please
    Tell Explain Describe Is Are Do Does Did The This That These Those If In On
    For And But A An My Our Your
    """.split()
)


def _unmatched_proper_nouns(
    question: str, passages: list[RetrievedPassage] | tuple[RetrievedPassage, ...]
) -> set[str]:
    """Names in the question that the retrieved passages do not mention.

    "What is the capital of France?" shares the word "capital" with the
    companies legislation, but no passage covers France. A named entity that
    appears nowhere in the evidence is a strong signal that the question is
    outside what the passage answers, so the caller should fall back rather
    than quote an unrelated provision.
    """
    names = {
        word
        for word in _PROPER_NOUN.findall(question)
        if word not in _SENTENCE_STARTERS
    }
    if not names:
        return set()
    haystack = " ".join(
        f"{passage.document_title} {passage.section_heading} {passage.text}"
        for passage in passages
    ).lower()
    return {
        name
        for name in names
        if not term_in_text(name.lower(), haystack, prefix_length=5)
    }


def _manual_passages_mention(
    passages: list[RetrievedPassage] | tuple[RetrievedPassage, ...],
    query: str,
) -> bool:
    """Whether the chosen manual passages mention a distinctive query term.

    The guide is searched first for every question, but a weak keyword overlap
    can retrieve an unrelated page (for example the Logout section for a
    question about affidavits). An implicit question is only answered from the
    guide when one of its meaningful terms actually appears in the selected
    passages or their headings; otherwise the caller falls back to the legal
    corpus. A question made only of stop words (a terse follow-up) is trusted to
    the conversation context instead.
    """
    terms = query_terms(query)
    if not terms:
        return True
    haystack = " ".join(
        f"{passage.section_heading} {passage.text}" for passage in passages
    ).lower()
    # A cheap stem check: "register" must match "registration", but a short
    # word such as "pay" must still appear as written. Comparing the first
    # five characters handles the common inflections without a stemmer.
    def present(term: str) -> bool:
        return term_in_text(term, haystack, prefix_length=5)

    # Answer only when the guide actually covers every meaningful term. A
    # question such as "registration of companies" must not be answered from
    # the IECMS account-registration page merely because both share "register".
    return all(present(term) for term in terms)


def _dedupe_labelled_passages(
    labelled: list[tuple[str, RetrievedPassage]],
) -> list[tuple[str, RetrievedPassage]]:
    """Keep one citation per page/section, preserving the first label.

    Extractive manual answers use several adjacent passages from the same page.
    Showing the same page twelve times is noise, not traceability.
    """
    seen: set[tuple[str, int | str]] = set()
    result: list[tuple[str, RetrievedPassage]] = []
    for label, passage in labelled:
        # One citation per PDF page. For sources without page numbers (for
        # example a text manual), keep distinct section headings instead.
        location: int | str = (
            passage.page_number
            if passage.page_number is not None
            else passage.section_heading
        )
        key = (passage.source_id, location)
        if key in seen:
            continue
        seen.add(key)
        result.append((label, passage))
    return result


def _manual_response(
    *,
    question: str,
    language: str,
    intent: IntentResult,
    status: str,
    answer: str,
    sources: list[dict],
    retrieval_confidence: str,
    steps: list[str],
    redirect_note: str = "",
    diagnostics: list[str] | None = None,
) -> dict:
    """Build a manual answer with the version caveat and routing metadata."""
    text = answer
    if redirect_note:
        text = f"{text}\n\n{redirect_note}"
    if retrieval_confidence in {"low", "medium"} and status != STATUS_INSUFFICIENT:
        text = f"{text}\n\n{IECMS_MANUAL_LOW_CONFIDENCE_CAVEAT}"
    return _payload(
        status=status,
        question=question,
        language=language,
        answer=text,
        sources=sources,
        notice=getattr(settings, "IECMS_MANUAL_VERSION_NOTE", IECMS_MANUAL_CAVEAT),
        extra={
            "label": IECMS_MANUAL_LABEL,
            "intent": intent.intent,
            "is_follow_up": intent.is_follow_up,
            "manual": True,
            "steps": steps,
            "confidence": retrieval_confidence,
            "diagnostics": diagnostics or [],
        },
    )


def _answer_from_manual(
    *,
    question: str,
    language: str,
    intent: IntentResult,
    redirect: bool,
) -> dict:
    """Answer an IECMS how-to question from the manual collection only.

    There is deliberately no open-web fallback here. A random web page is not a
    trustworthy source for what a specific IECMS button or menu does, and the
    assistant must not invent system behaviour.
    """
    retrieval = retrieval_service.get_manual_retrieval_service().search(
        intent.effective_query
    )
    redirect_note = safety_service.REDIRECT_MESSAGE if redirect else ""

    if not retrieval.sufficient:
        return _manual_response(
            question=question,
            language=language,
            intent=intent,
            status=STATUS_INSUFFICIENT,
            answer=IECMS_MANUAL_NOT_COVERED_TEXT,
            sources=[],
            retrieval_confidence=retrieval.confidence,
            steps=[],
            redirect_note=redirect_note,
            diagnostics=list(retrieval.diagnostics),
        )

    passages = retrieval.passages

    # An implicit IECMS question ("How do I prepare an affidavit?") is only
    # answered from the guide when the retrieved passages actually mention a
    # meaningful term from the question. Otherwise a weak keyword overlap can
    # produce an unrelated page (for example the Logout section). Explicit
    # IECMS questions keep the manual's own honest "not covered" answer.
    if not intent.explicit_manual and not _manual_passages_mention(
        passages, intent.effective_query
    ):
        return _manual_response(
            question=question,
            language=language,
            intent=intent,
            status=STATUS_INSUFFICIENT,
            answer=IECMS_MANUAL_NOT_COVERED_TEXT,
            sources=[],
            retrieval_confidence=retrieval.confidence,
            steps=[],
            redirect_note=redirect_note,
            diagnostics=[
                "The retrieved guide passages did not mention the key terms "
                "of the question.",
                *retrieval.diagnostics,
            ],
        )

    labelled: list[tuple[str, RetrievedPassage]] = [
        (f"S{index}", passage)
        for index, passage in enumerate(passages, start=1)
    ]

    if llm_service.is_configured():
        try:
            generated, validation = _generate_and_validate(
                question=question,
                labelled=labelled,
                language=language,
                system_instruction=safety_service.get_manual_system_instructions(),
                redirect=redirect,
                context=intent.context_note,
            )
        except llm_service.LLMServiceError as exc:
            logger.warning("Manual explanation engine failed: code=%s", exc.code)
            return _payload(
                status=STATUS_PROVIDER_ERROR,
                question=question,
                language=language,
                answer=str(exc),
                extra={
                    "error_code": exc.code,
                    "intent": intent.intent,
                    "is_follow_up": intent.is_follow_up,
                    "manual": True,
                },
            )

        if validation.invalid_labels:
            logger.warning(
                "Dropped %s fabricated citation marker(s) from the manual answer.",
                len(validation.invalid_labels),
            )

        if not validation.has_citations:
            # The model either returned the sentinel or invented references.
            # Do not show an ungrounded procedure.
            return _manual_response(
                question=question,
                language=language,
                intent=intent,
                status=STATUS_INSUFFICIENT,
                answer=IECMS_MANUAL_NOT_COVERED_TEXT,
                sources=[],
                retrieval_confidence=retrieval.confidence,
                steps=[],
                redirect_note=redirect_note,
                diagnostics=[
                    "The generated manual answer did not cite a retrieved "
                    "manual passage, so it was not shown.",
                    *retrieval.diagnostics,
                ],
            )

        steps = procedure_service.extract_steps(validation.text)
        return _manual_response(
            question=question,
            language=language,
            intent=intent,
            status=STATUS_OK,
            answer=validation.text,
            sources=validation.to_dicts(),
            retrieval_confidence=retrieval.confidence,
            steps=steps,
            redirect_note=redirect_note,
            diagnostics=list(retrieval.diagnostics),
        )

    # Extractive mode: quote the manual rather than inventing a summary.
    if not settings.LLM_ALLOW_EXTRACTIVE_FALLBACK:
        return _payload(
            status=STATUS_PROVIDER_ERROR,
            question=question,
            language=language,
            answer=(
                "The explanation service is not available because no language "
                "model is configured. Please contact the administrator."
            ),
            extra={
                "error_code": "not_configured",
                "intent": intent.intent,
                "is_follow_up": intent.is_follow_up,
                "manual": True,
            },
        )

    answer_text, used = _manual_extractive_answer(passages)
    used_labels = [
        (label, passage)
        for label, passage in labelled
        if passage.passage_id in {item.passage_id for item in used}
    ]
    validation = citation_service.citations_for(
        _dedupe_labelled_passages(used_labels)
    )
    steps = procedure_service.extract_steps(answer_text)
    return _manual_response(
        question=question,
        language=language,
        intent=intent,
        status=STATUS_EXTRACTIVE,
        answer=answer_text,
        sources=validation.to_dicts(),
        retrieval_confidence=retrieval.confidence,
        steps=steps,
        redirect_note=redirect_note,
        diagnostics=list(retrieval.diagnostics),
    )


def answer_question(
    question: str,
    language: str = DEFAULT_LANGUAGE,
    history=None,
) -> dict:
    """Produce a grounded explanation for ``question``.

    ``history`` is an optional, bounded list of previous questions supplied by
    the browser. It is used only to resolve follow-ups and is never stored.
    The ``language`` argument is retained for compatibility with older clients
    but output is English-only. Always returns a payload; failures are reported
    as explicit statuses rather than exceptions.
    """
    resolved = DEFAULT_LANGUAGE

    # 1. Privacy: reject before anything leaves the process.
    privacy = privacy_service.scan(question)
    if privacy.detected:
        return _payload(
            status=STATUS_REJECTED_PRIVACY,
            question=question,
            language=resolved,
            answer=privacy.message(),
            extra={"privacy_categories": list(privacy.categories)},
        )

    # 2. Intent detection. This is deterministic and local: no question is
    # sent anywhere merely to decide which approved collection to search.
    history = _clean_history(history)
    intent = intent_service.classify(question, history)

    # 3. Legal-safety screening. In an IECMS how-to context, phrases such as
    # "my case" or "what should I do if this step fails?" are about using the
    # system, so only the genuinely legal-advice categories still apply.
    verdict = safety_service.evaluate_question(
        question, procedural=intent.is_iecms
    )
    if verdict.blocks:
        return _payload(
            status=STATUS_REFUSED,
            question=question,
            language=resolved,
            answer=safety_service.REFUSAL_MESSAGE,
            extra={
                "safety_categories": list(verdict.categories),
                "intent": intent.intent,
                "is_follow_up": intent.is_follow_up,
            },
        )

    # The chat message itself cannot carry a file. Point at the upload button
    # rather than pretending a document was read from the question.
    if intent.intent == intent_service.INTENT_DOCUMENT_SUMMARY:
        return _payload(
            status=STATUS_INSUFFICIENT,
            question=question,
            language=resolved,
            answer=DOCUMENT_SUMMARY_GUIDANCE_TEXT,
            extra={
                "intent": intent.intent,
                "is_follow_up": intent.is_follow_up,
                "diagnostics": ["No document was attached to the chat message."],
            },
        )

    # 4. IECMS how-to questions prefer the manual collection. When the manual
    # has nothing, search the web rather than stopping: the result is clearly
    # labelled unverified, and the verifier panel checks it before display.
    web_only = False
    redirect_note = verdict.redirect_message() if verdict.redirect else ""
    if intent.is_iecms:
        if settings.ENABLE_IECMS_MANUAL_ASSISTANT:
            manual_result = _answer_from_manual(
                question=question,
                language=resolved,
                intent=intent,
                redirect=verdict.redirect,
            )
            if manual_result["status"] != STATUS_INSUFFICIENT:
                return manual_result
            if intent.explicit_manual:
                # The guide has nothing. Search the web (then general
                # knowledge), still labelling everything unverified.
                web_only = True
            # An implicit question may be an ordinary legal procedure
            # question, so it falls through to the legal corpus below.
        elif intent.explicit_manual:
            # Manual assistance is disabled, but the user named IECMS: use
            # the web fallback instead of claiming the guide is unavailable.
            web_only = True

    # 4b. The companion is an IECMS assistant, so an ordinary question is
    # offered to the guide even when the user did not name the system.
    # Definitions are deliberately excluded: asking "What is a party?" wants
    # the legal meaning, not the "Adding Parties" procedure. A weak manual
    # match falls through to the legal corpus.
    if (
        intent.intent == intent_service.INTENT_LEGAL_GENERAL
        and settings.ENABLE_IECMS_MANUAL_ASSISTANT
    ):
        manual_result = _answer_from_manual(
            question=question,
            language=resolved,
            intent=intent,
            redirect=verdict.redirect,
        )
        if (
            manual_result["status"] != STATUS_INSUFFICIENT
            and manual_result.get("confidence") in {"high", "medium"}
        ):
            return manual_result

    # 5. Retrieval from the general legal corpus. For an explicit IECMS
    # question the manual had nothing, so the legal corpus is irrelevant: skip
    # it and go straight to the web/general-knowledge fallback.
    from_web = False
    if web_only:
        retrieval = RetrievalResult(query=intent.effective_query)
        weak_local_match = False
        weak_match_diagnostics = [
            "The approved IECMS manual does not cover this question."
        ]
    else:
        retrieval = retrieval_service.get_retrieval_service().search(
            intent.effective_query
        )
        # A passage that merely shares an incidental word is not evidence. Only
        # a match that covers a meaningful part of the question may ground an
        # answer; otherwise the assistant says it does not know instead of
        # quoting something unrelated.
        weak_local_match = (
            retrieval.sufficient and retrieval.confidence in {"low", "none"}
        )
        weak_match_diagnostics = (
            [
                "The closest local passage matched only an incidental word, so "
                "it was not used as evidence."
            ]
            if weak_local_match
            else []
        )

    if retrieval.sufficient and not weak_local_match:
        passages = retrieval.passages
        missing_names = _unmatched_proper_nouns(question, passages)
        if missing_names:
            weak_local_match = True
            weak_match_diagnostics.append(
                "The question names "
                + ", ".join(sorted(missing_names))
                + ", which the matched passage does not cover."
            )
        else:
            confidence = retrieval.confidence

    if retrieval.sufficient and not weak_local_match:
        passages = retrieval.passages
    elif settings.ENABLE_WEB_FALLBACK:
        # The approved corpus does not answer this. Search the open web instead
        # of refusing, but label the result unverified everywhere it appears.
        outcome = web_search_service.search_and_fetch(intent.effective_query)
        if not outcome.has_evidence:
            general = _general_knowledge_payload(
                question=question,
                language=resolved,
                intent=intent,
                redirect_note=redirect_note,
            )
            if general is not None:
                return general
            return _no_answer_payload(
                question=question,
                language=resolved,
                retrieval=retrieval,
                diagnostics=[*weak_match_diagnostics, *outcome.diagnostics],
                redirect=verdict.redirect,
                intent=intent,
            )
        passages = tuple(web_search_service.build_web_passages(outcome))
        if not passages:
            # Search results existed but none of their text addressed the
            # question. Do not hand an unrelated page to the model.
            general = _general_knowledge_payload(
                question=question,
                language=resolved,
                intent=intent,
                redirect_note=redirect_note,
            )
            if general is not None:
                return general
            return _no_answer_payload(
                question=question,
                language=resolved,
                retrieval=retrieval,
                diagnostics=[
                    *weak_match_diagnostics,
                    "Web results were found, but none addressed the question.",
                ],
                redirect=verdict.redirect,
                intent=intent,
            )
        confidence = "web"
        from_web = True
    else:
        general = _general_knowledge_payload(
            question=question,
            language=resolved,
            intent=intent,
            redirect_note=redirect_note,
        )
        if general is not None:
            return general
        return _no_answer_payload(
            question=question,
            language=resolved,
            retrieval=retrieval,
            diagnostics=weak_match_diagnostics,
            redirect=verdict.redirect,
            intent=intent,
        )

    labelled: list[tuple[str, RetrievedPassage]] = [
        (f"S{index}", passage) for index, passage in enumerate(passages, start=1)
    ]

    # 6. Explanation: provider, or verbatim quotation when none is configured.
    if llm_service.is_configured():
        system_instruction = safety_service.get_system_instructions()
        try:
            generated, validation = _generate_and_validate(
                question=question,
                labelled=labelled,
                language=resolved,
                system_instruction=system_instruction,
                redirect=verdict.redirect,
                context=intent.context_note,
            )
        except llm_service.LLMServiceError as exc:
            logger.warning("Explanation engine failed: code=%s", exc.code)
            return _payload(
                status=STATUS_PROVIDER_ERROR,
                question=question,
                language=resolved,
                answer=str(exc),
                extra={
                    "error_code": exc.code,
                    "intent": intent.intent,
                    "is_follow_up": intent.is_follow_up,
                },
            )

        # The model may judge that the approved passages do not answer the
        # question; that surfaces as an answer with no valid citation. This is
        # exactly the case the web fallback exists for, so give it one chance
        # before refusing.
        if not validation.has_citations and not from_web and settings.ENABLE_WEB_FALLBACK:
            web_passages = tuple(
                web_search_service.build_web_passages(
                    web_search_service.search_and_fetch(intent.effective_query)
                )
            )
            if web_passages:
                passages = web_passages
                labelled = [
                    (f"S{index}", passage)
                    for index, passage in enumerate(passages, start=1)
                ]
                from_web = True
                confidence = "web"
                try:
                    generated, validation = _generate_and_validate(
                        question=question,
                        labelled=labelled,
                        language=resolved,
                        system_instruction=system_instruction,
                        redirect=verdict.redirect,
                        context=intent.context_note,
                    )
                except llm_service.LLMServiceError as exc:
                    logger.warning("Web fallback generation failed: code=%s", exc.code)

        if validation.invalid_labels:
            # The model invented a reference. The fabricated markers are already
            # stripped; record the event without logging the answer text.
            logger.warning(
                "Dropped %s fabricated citation marker(s) from the model answer.",
                len(validation.invalid_labels),
            )

        if not validation.has_citations:
            # An answer with no valid citation is not grounded; do not present
            # it as one. If the model is available, try a verified general
            # answer instead of stopping here.
            general = _general_knowledge_payload(
                question=question,
                language=resolved,
                intent=intent,
                redirect_note=redirect_note,
            )
            if general is not None:
                return general
            return _payload(
                status=STATUS_INSUFFICIENT,
                question=question,
                language=resolved,
                answer=INSUFFICIENT_EVIDENCE_TEXT,
                extra={
                    "diagnostics": [
                        "The generated answer did not cite any retrieved source, "
                        "so it was not shown as a grounded explanation."
                    ],
                    "intent": intent.intent,
                    "is_follow_up": intent.is_follow_up,
                },
            )

        verification_meta = None
        if (
            from_web
            and settings.ENABLE_ANSWER_VERIFICATION
            and llm_service.is_configured()
        ):
            try:
                review = verification_service.verify_answer(
                    question=question,
                    draft=validation.text,
                    evidence=_verification_evidence(labelled),
                    source_kind="web",
                )
            except verification_service.VerificationUnavailable:
                logger.warning("Web answer could not be verified.")
                return _payload(
                    status=STATUS_PROVIDER_ERROR,
                    question=question,
                    language=resolved,
                    answer=VERIFICATION_FAILED_TEXT,
                    extra={
                        "error_code": "verification_failed",
                        "intent": intent.intent,
                        "is_follow_up": intent.is_follow_up,
                    },
                )

            if not review.approved:
                logger.warning("Web answer was rejected by the verification panel.")
                return _payload(
                    status=STATUS_INSUFFICIENT,
                    question=question,
                    language=resolved,
                    answer=INSUFFICIENT_EVIDENCE_TEXT,
                    extra={
                        "diagnostics": [
                            "A web-based draft was produced but the "
                            "verification panel did not approve it, so it was "
                            "not shown."
                        ],
                        "intent": intent.intent,
                        "is_follow_up": intent.is_follow_up,
                    },
                )

            # A revision may have dropped or altered the citation markers.
            revalidated = citation_service.validate_citations(
                review.text, labelled
            )
            if not revalidated.has_citations:
                return _payload(
                    status=STATUS_INSUFFICIENT,
                    question=question,
                    language=resolved,
                    answer=INSUFFICIENT_EVIDENCE_TEXT,
                    extra={
                        "diagnostics": [
                            "The verified revision did not cite a retrieved "
                            "source, so it was not shown."
                        ],
                        "intent": intent.intent,
                        "is_follow_up": intent.is_follow_up,
                    },
                )
            validation = revalidated
            answer_text = review.text
            verification_meta = _verification_meta(review)
        else:
            answer_text = validation.text

        if redirect_note:
            answer_text = f"{answer_text}\n\n{redirect_note}"
        if from_web:
            caveat = WEB_FALLBACK_CAVEAT
        else:
            caveat = LOW_CONFIDENCE_CAVEAT if confidence in {"low", "medium"} else ""
        if caveat:
            answer_text = f"{answer_text}\n\n{caveat}"

        return _payload(
            status=STATUS_WEB_FALLBACK if from_web else STATUS_OK,
            question=question,
            language=resolved,
            answer=answer_text,
            sources=validation.to_dicts(),
            notice=WEB_FALLBACK_NOTICE if from_web else "",
            extra={
                "model": generated.model,
                "provider": generated.provider,
                "redirected": verdict.redirect,
                "synthetic_warning": _synthetic_warning(passages),
                "dropped_citations": len(validation.invalid_labels),
                "confidence": confidence,
                "confidence_caveat": caveat,
                "web_fallback": from_web,
                "verification": verification_meta,
                "intent": intent.intent,
                "is_follow_up": intent.is_follow_up,
            },
        )

    # Extractive fallback: a short, single-source answer. The full reference
    # (title, authority, link) is returned in `sources`, not dumped in the text.
    if not settings.LLM_ALLOW_EXTRACTIVE_FALLBACK:
        return _payload(
            status=STATUS_PROVIDER_ERROR,
            question=question,
            language=resolved,
            answer=(
                "The explanation service is not available because no language "
                "model is configured. Please contact the administrator."
            ),
            extra={
                "error_code": "not_configured",
                "intent": intent.intent,
                "is_follow_up": intent.is_follow_up,
            },
        )

    top_label, top_passage = labelled[0]
    answer_text = _extractive_answer([top_passage])
    if redirect_note:
        answer_text = f"{answer_text}\n\n{redirect_note}"

    validation = citation_service.citations_for([(top_label, top_passage)])

    notice = WEB_FALLBACK_NOTICE if from_web else ""

    return _payload(
        status=STATUS_EXTRACTIVE,
        question=question,
        language=resolved,
        answer=answer_text,
        sources=validation.to_dicts(),
        notice=notice,
        extra={
            "redirected": verdict.redirect,
            "synthetic_warning": _synthetic_warning((top_passage,)),
            "confidence": confidence,
            "confidence_caveat": "",
            "web_fallback": from_web,
            "intent": intent.intent,
            "is_follow_up": intent.is_follow_up,
        },
    )


def _generate_and_validate(
    *,
    question: str,
    labelled: list[tuple[str, RetrievedPassage]],
    language: str,
    system_instruction: str,
    redirect: bool,
    context: str = "",
) -> tuple[llm_service.LLMAnswer, "citation_service.CitationValidation"]:
    """Generate an explanation and validate its citations against the passages."""
    generated = llm_service.generate_explanation(
        question=question,
        labelled_passages=labelled,
        language=language,
        system_instruction=system_instruction,
        redirect=redirect,
        context=context,
    )
    if NO_ANSWER_SENTINEL in generated.text:
        # The model reported that the sources do not answer the question. Treat
        # it as ungrounded so the caller can try the web fallback.
        return generated, citation_service.CitationValidation(text="")
    validation = citation_service.validate_citations(generated.text, labelled)
    return generated, validation


def _no_answer_payload(
    *,
    question: str,
    language: str,
    retrieval: RetrievalResult,
    diagnostics: list[str],
    redirect: bool,
    intent: IntentResult | None = None,
) -> dict:
    """The honest refusal used when nothing approved (or on the web) answers."""
    return _payload(
        status=STATUS_INSUFFICIENT,
        question=question,
        language=language,
        answer=INSUFFICIENT_EVIDENCE_TEXT,
        extra={
            "diagnostics": [*retrieval.diagnostics, *diagnostics],
            "redirected": redirect,
            "confidence": retrieval.confidence,
            "web_fallback": False,
            "intent": intent.intent if intent else "",
            "is_follow_up": intent.is_follow_up if intent else False,
        },
    )

