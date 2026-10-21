"""Light-weight intent detection and follow-up resolution.

The chatbot has two distinct evidence collections:

- the legal definitions corpus, and
- the IECMS procedural manuals.

A keyword router is used rather than an extra model call: it is deterministic,
testable, cheap, and it keeps the privacy boundary intact (no question is sent
to a provider just to decide where to look). The router is deliberately
conservative: only clearly procedural questions are routed to the manual, so
existing definition behaviour is preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INTENT_LEGAL_DEFINITION = "legal_definition"
INTENT_LEGAL_GENERAL = "legal_general"
INTENT_IECMS_HOW_TO = "iecms_how_to"
INTENT_IECMS_MANUAL_SECTION = "iecms_manual_section"
INTENT_DOCUMENT_SUMMARY = "document_summary"
INTENT_UNSUPPORTED = "unsupported"

IECMS_INTENTS = frozenset(
    {INTENT_IECMS_HOW_TO, INTENT_IECMS_MANUAL_SECTION}
)

_IECMS = re.compile(
    r"\b(?:iecms|integrated\s+electronic\s+case\s+management(?:\s+system)?)\b",
    re.IGNORECASE,
)

# Phrases that are specific to the IECMS workflow. They are strong enough to
# identify a procedural question even when the user does not type "IECMS".
_STRONG_MANUAL = re.compile(
    r"\b(?:my\s+workspace|pending\s+actions|case\s+records?|"
    r"add\s+(?:a\s+|the\s+)?part(?:y|ies)|legal\s+entit(?:y|ies)|"
    r"file\s+(?:a\s+|the\s+)?case|create\s+(?:a\s+|the\s+)?case|"
    r"submit\s+(?:a\s+|the\s+)?case|"
    r"sign(?:ing)?\s+(?:a\s+|the\s+)?documents?|"
    r"merge\s+(?:the\s+)?documents?|new\s+signature|saved\s+signature|"
    r"upload\s+(?:a\s+|the\s+)?documents?|"
    r"payment\s+receipt|payment\s+status|calendar|notifications?|"
    r"export\s+(?:the\s+)?calendar|reset\s+(?:my\s+)?password|"
    r"forgot\s+(?:my\s+)?password|create\s+(?:an\s+)?account|"
    r"register\s+(?:an\s+)?account|sign\s+up|log\s*in|log\s*out|"
    r"logout|login)\b",
    re.IGNORECASE,
)

_WORKFLOW_TERM = re.compile(
    r"\b(?:file|filing|submit|submission|register|registration|account|"
    r"login|log\s*in|logout|log\s*out|password|profile|signature|sign|"
    r"merge|merging|payment|receipt|workspace|notification|calendar|"
    r"case\s+records?|pending\s+actions|dashboard|party|parties)\b",
    re.IGNORECASE,
)

_HOW_TO = re.compile(
    r"\b(?:how\s+(?:do|can|should)\s+i|how\s+to|steps?\s+to|"
    r"where\s+do\s+i|what\s+do\s+i\s+(?:click|enter|select))\b",
    re.IGNORECASE,
)

_MANUAL_REFERENCE = re.compile(
    r"\b(?:manual|guide|handbook|documentation|page|section)\b",
    re.IGNORECASE,
)

# Wording that names the IECMS guide itself. Such a question must be answered
# from the manual and must not silently fall back to the legal corpus.
_EXPLICIT_MANUAL = re.compile(
    r"\b(?:iecms|integrated\s+electronic\s+case\s+management(?:\s+system)?|"
    r"manual|guide|handbook|documentation)\b",
    re.IGNORECASE,
)

_DEFINITION = re.compile(
    r"\bwhat\s+is\b|\bwhat\s+are\b|\bwhat\s+does\b[^?]{0,80}\bmean\b|"
    r"\bmeaning\s+of\b|\bdefine\b|\bdefinition\s+of\b|"
    r"\bdifference\s+between\b|\bexplain\s+(?:the\s+)?(?:term|meaning)\b",
    re.IGNORECASE,
)

_SUMMARY = re.compile(
    r"\b(?:summari[sz]e|summarise|summary|key\s+points|main\s+points|"
    r"uploaded\s+document|attached\s+document|my\s+document|this\s+document)\b",
    re.IGNORECASE,
)

_FOLLOW_UP_START = re.compile(
    r"^(?:and|also|then|next|what\s+about|how\s+about|what\s+if|why|where|"
    r"when|which|can\s+i|do\s+i|does\s+it|is\s+it|explain\s+the\s+next\s+step|"
    r"what\s+does\s+this|what\s+should\s+i\s+do\s+if)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IntentResult:
    """The detected intent plus the query used for retrieval."""

    intent: str
    question: str
    effective_query: str
    is_follow_up: bool = False
    context_note: str = ""
    previous_intent: str = ""
    # The user named IECMS or its guide. Implicit questions may fall back to
    # the legal corpus when the manual has nothing; explicit ones must not.
    explicit_manual: bool = False

    @property
    def is_iecms(self) -> bool:
        return self.intent in IECMS_INTENTS


def _history_entry_question(entry) -> str:
    if isinstance(entry, dict):
        value = entry.get("question", "")
    else:
        value = getattr(entry, "question", "")
    return " ".join(str(value).split()) if value else ""


def _history_entry_intent(entry) -> str:
    if isinstance(entry, dict):
        value = entry.get("intent", "")
    else:
        value = getattr(entry, "intent", "")
    return str(value) if value else ""


def _previous_question(history) -> str:
    for entry in reversed(list(history or [])):
        question = _history_entry_question(entry)
        if question:
            return question
    return ""


def _previous_intent(history) -> str:
    for entry in reversed(list(history or [])):
        question = _history_entry_question(entry)
        if not question:
            continue
        recorded = _history_entry_intent(entry)
        if recorded in {
            INTENT_LEGAL_DEFINITION,
            INTENT_LEGAL_GENERAL,
            INTENT_IECMS_HOW_TO,
            INTENT_IECMS_MANUAL_SECTION,
            INTENT_DOCUMENT_SUMMARY,
            INTENT_UNSUPPORTED,
        }:
            return recorded
        # The client is not required to send an intent; infer it from the
        # previous question without following the history chain recursively.
        return classify(question).intent
    return ""


def _is_definition(text: str) -> bool:
    return bool(_DEFINITION.search(text))


def _is_manual_question(text: str, previous_intent: str) -> bool:
    if _IECMS.search(text):
        return True
    if _STRONG_MANUAL.search(text):
        return True
    if _MANUAL_REFERENCE.search(text) and _WORKFLOW_TERM.search(text):
        return True
    # The companion is an IECMS assistant, so a how-to or workflow question is
    # about the system even when the user does not type "IECMS". Definition
    # questions are excluded by the caller before this point.
    if _HOW_TO.search(text) or _WORKFLOW_TERM.search(text):
        return True
    # A follow-up in an ongoing manual conversation stays in that conversation
    # even if it does not repeat the word "IECMS" or a workflow term.
    if previous_intent in IECMS_INTENTS:
        if _WORKFLOW_TERM.search(text) or _FOLLOW_UP_START.search(text):
            return True
    return False


def classify(question: str, history=None) -> IntentResult:
    """Classify a question and resolve the retrieval query.

    ``history`` is a bounded list of previous ``{question, intent}`` entries
    supplied by the browser. It is only used to resolve pronouns and short
    follow-ups; it is never stored.
    """
    normalised = " ".join((question or "").split())
    lowered = normalised.lower()
    previous_question = _previous_question(history)
    previous_intent = _previous_intent(history)
    words = normalised.split()

    is_summary = bool(_SUMMARY.search(lowered))
    is_definition = _is_definition(lowered)
    manual = _is_manual_question(lowered, previous_intent)
    manual_section = manual and bool(_MANUAL_REFERENCE.search(lowered))

    # "What is a case record?" is a definition question, not a request for
    # IECMS instructions. Only an explicit IECMS/manual reference overrides
    # that distinction.
    if is_definition and not _IECMS.search(lowered) and not _MANUAL_REFERENCE.search(lowered):
        manual = False
        manual_section = False

    is_follow_up = False
    if previous_question and not is_summary:
        starts_like_follow_up = bool(_FOLLOW_UP_START.search(lowered))
        short_same_domain = (
            len(words) <= 8
            and manual
            and previous_intent in IECMS_INTENTS
        )
        short_definition_follow_up = (
            len(words) <= 8
            and is_definition
            and previous_intent == INTENT_LEGAL_DEFINITION
            and starts_like_follow_up
        )
        if previous_intent in IECMS_INTENTS and starts_like_follow_up:
            is_follow_up = True
        elif not is_definition:
            is_follow_up = starts_like_follow_up or short_same_domain
        else:
            is_follow_up = short_definition_follow_up

    if is_summary:
        intent = INTENT_DOCUMENT_SUMMARY
    elif manual_section:
        intent = INTENT_IECMS_MANUAL_SECTION
    elif manual:
        intent = INTENT_IECMS_HOW_TO
    elif is_definition:
        intent = INTENT_LEGAL_DEFINITION
    elif _IECMS.search(lowered):
        intent = INTENT_IECMS_HOW_TO
    elif _WORKFLOW_TERM.search(lowered) and _HOW_TO.search(lowered):
        # A workflow question without the "IECMS" name. Keep it in the legal
        # corpus unless the wording is IECMS-specific; the manual must not
        # absorb general court procedure questions.
        intent = INTENT_LEGAL_GENERAL
    else:
        intent = INTENT_LEGAL_GENERAL

    # A follow-up inherits the previous domain when the new wording does not
    # establish one on its own (for example "What does this option mean?").
    if is_follow_up and previous_intent in IECMS_INTENTS and not _IECMS.search(lowered):
        if intent in {INTENT_LEGAL_GENERAL, INTENT_LEGAL_DEFINITION}:
            intent = INTENT_IECMS_HOW_TO

    # Use the previous question for retrieval only when the new question is
    # genuinely ambiguous ("What does this option mean?", "Explain the next
    # step"). A self-contained follow-up such as "How do I add the parties?"
    # must not be dragged back to the earlier topic.
    ambiguous_follow_up = bool(
        re.match(
            r"^(?:what about|how about|what does this|what should i do if|"
            r"explain the next step|why|where|when|which|and|also|then|next)\b",
            lowered,
        )
    ) or (
        len(words) <= 8
        and bool(re.search(r"\b(?:it|this|that|they|them|there)\b", lowered))
    )

    effective_query = normalised
    context_note = ""
    if is_follow_up and previous_question:
        context_note = (
            "The user is following up on the earlier question: "
            f"{previous_question}"
        )
        if ambiguous_follow_up:
            effective_query = f"{previous_question} {normalised}".strip()

    return IntentResult(
        intent=intent,
        question=normalised,
        effective_query=effective_query,
        is_follow_up=is_follow_up,
        context_note=context_note,
        previous_intent=previous_intent,
        explicit_manual=bool(
            _EXPLICIT_MANUAL.search(lowered) or _STRONG_MANUAL.search(lowered)
        ),
    )
