"""Shared, non-secret constants for the chatbot app.

Nothing in this module is configuration that must vary per environment; that
lives in ``django.conf.settings`` (see ``config/settings.py`` and
``.env.example``).
"""

from __future__ import annotations

# The assistant answers in English. The field is retained in payloads for
# compatibility with existing clients, but there is no language selector and
# no translated output.
DEFAULT_LANGUAGE = "en"

# Bounded conversation context. The browser sends only previous question text
# (never answers), and the backend never stores it. These limits keep a
# follow-up useful without turning the request into a transcript upload.
MAX_HISTORY_ENTRIES = 6
MAX_HISTORY_QUESTION_CHARS = 500

# Example questions shown in the interface. They are deliberately generic
# terminology questions about fictional/general situations: they are not drawn
# from any real case record and are not legal advice.
EXAMPLE_QUESTIONS: list[str] = [
    "What is an affidavit?",
    "How do I file a case in IECMS?",
    "What does \u201cApplication for Condonation\u201d mean?",
    "How do I reset my password?",
    "What is a Notice of Set Down?",
    "How do I add the parties?",
    "What is the difference between an order of court and a judgment?",
]

# Shown next to every answer and in the page footer.
DISCLAIMER_TEXT = (
    "IECMS Companion provides general information about legal terminology, "
    "court procedure and how to use the Integrated Electronic Case Management "
    "System (IECMS). IECMS guidance is based on the published user guide and "
    "may not reflect the live system. It is not legal advice, it does not "
    "create a lawyer-client relationship, and it is not an official Judicial "
    "Service Commission service. For advice about your own situation, consult "
    "a qualified legal practitioner or an authorised court office."
)

# Shown near the input box.
PRIVACY_REMINDER = (
    "Do not type names, passwords, IECMS login details, national ID numbers, "
    "phone numbers, email addresses, home addresses, case numbers or any other "
    "personal or confidential details. This chatbot does not need them and "
    "does not store your messages."
)

# Shown while the real explanation engine is not yet connected (Phase 1).
MOCK_MODE_NOTICE = (
    "Development mode: answers are clearly-labelled placeholders. No language "
    "model is called and no approved legal source is searched yet."
)

# Shown when no LLM is configured and the answer is a verbatim quotation of the
# retrieved source rather than a generated explanation.
EXTRACTIVE_MODE_NOTICE = (
    "No language model is configured, so this answer quotes the retrieved "
    "source directly instead of explaining it. The quotation is reproduced "
    "as-is from the stored document."
)

# Shown when any part of the answer rests on a synthetic test fixture.
SYNTHETIC_WARNING = (
    "This answer is based on a synthetic test fixture, not an official legal "
    "source, so it must not be relied on."
)

# Used when the approved material does not establish a reliable answer.
INSUFFICIENT_EVIDENCE_TEXT = (
    "The approved material available to me does not establish a reliable "
    "answer to that question. I have not found an approved source that "
    "covers it, so I will not guess or rely on general knowledge. For "
    "guidance on a specific situation, please consult a qualified legal "
    "practitioner or an authorised court office."
)

# Added when the retrieved passage only partly matches the question. Keyword
# search can match on an incidental word, so a partial match is disclosed.
LOW_CONFIDENCE_CAVEAT = (
    "Note: the source below matched only part of your question, so it may not "
    "answer it directly. Try rephrasing using the term you want explained."
)

# Added when the answer could only be built from an open-web search because no
# approved source covered the question. The user must be able to tell.
WEB_FALLBACK_NOTICE = (
    "No approved source covered this question, so this answer was built from an "
    "open-web search. Those pages are NOT approved official sources and have "
    "not been verified. Check each link below before relying on it."
)

WEB_FALLBACK_CAVEAT = (
    "This answer rests on unverified web pages rather than the approved source "
    "collection, so treat it as a starting point, not as authority."
)

# Used when nothing approved or on the web answers the question and the model
# is allowed to answer from its own general knowledge. The distinction from a
# sourced answer must always be visible.
GENERAL_KNOWLEDGE_LABEL = "GENERAL KNOWLEDGE (UNVERIFIED)"
GENERAL_KNOWLEDGE_NOTICE = (
    "No approved source and no reliable web page answered this question, so "
    "this answer comes from the assistant's general knowledge. It is NOT from "
    "an approved or official source, and it has not been fact-checked against "
    "one. Treat it as a starting point and confirm anything important."
)
GENERAL_KNOWLEDGE_CAVEAT = (
    "General knowledge only \u2014 unverified and not an official source."
)

# Shown when the verifier agents cannot reach a decision, so an unverified
# generated draft is not shown.
VERIFICATION_FAILED_TEXT = (
    "I found material that might answer this, but it could not be verified "
    "before it was sent, so I have not shown it. Please try again, or consult "
    "a qualified legal practitioner or an authorised court office."
)

# --- IECMS manual (procedural guidance) ---------------------------------

# Badge used for answers grounded in an IECMS manual rather than in a legal
# authority. The source panel still shows the manual title and page.
IECMS_MANUAL_LABEL = "IECMS GUIDE"

# Added when the manual passage only partly matches the question.
IECMS_MANUAL_LOW_CONFIDENCE_CAVEAT = (
    "Note: the manual match was partial. Check the cited page or pages before "
    "relying on these steps, and confirm them in the current IECMS interface."
)

# Appended to manual-based answers. The manual is a 2021 publication and the
# live system may have changed; the assistant must not imply otherwise.
IECMS_MANUAL_CAVEAT = (
    "This answer is based on the IECMS Getting Started Guide \u2014 Public "
    "Users, Version 1.1 (2021). The live IECMS interface and its procedures may "
    "have changed since publication; confirm the current steps in IECMS."
)

# Used when the manual collection does not cover an IECMS question. The
# assistant must not fall back to open-web instructions for system operations.
IECMS_MANUAL_NOT_COVERED_TEXT = (
    "The approved IECMS manual available to me does not describe that. I "
    "can only give steps that are documented in the manual, and I will not "
    "invent buttons, menu names or workflows. Please check the current "
    "IECMS interface or contact an authorised court office."
)

# The chat itself cannot receive a file. The intent detector recognises a
# summarisation request and points at the upload control instead of pretending
# a document was read from the message.
DOCUMENT_SUMMARY_GUIDANCE_TEXT = (
    "To summarise a document, use the Upload document button under the "
    "question box. I can read PDF, Word, plain-text, Markdown and HTML "
    "files there. Do not upload documents containing personal or "
    "confidential details: uploaded files are processed temporarily and "
    "then deleted."
)
