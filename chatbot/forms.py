"""Forms used to validate chat input.

The chat endpoint receives JSON, but the validation rules are declared as a
Django form so that they live in one place and can be unit tested without going
through the HTTP layer.
"""

from __future__ import annotations

from django import forms
from django.conf import settings

from .constants import MAX_HISTORY_ENTRIES, MAX_HISTORY_QUESTION_CHARS


class ChatRequestForm(forms.Form):
    """Validate a single terminology or IECMS usage question."""

    question = forms.CharField(
        min_length=settings.CHAT_MIN_QUESTION_LENGTH,
        max_length=settings.CHAT_MAX_QUESTION_LENGTH,
        strip=True,
        error_messages={
            "required": "Please enter a question.",
            "min_length": "Please enter a longer question.",
            "max_length": (
                "Questions must be at most "
                f"{settings.CHAT_MAX_QUESTION_LENGTH} characters long."
            ),
        },
    )

    # Bounded follow-up context from the browser. Only previous question text
    # and an optional intent are accepted; answers are never sent back.
    history = forms.JSONField(required=False)

    def clean_question(self) -> str:
        """Collapse whitespace runs so the length limit cannot be padded around."""
        question = self.cleaned_data["question"]
        return " ".join(question.split())

    def clean_history(self) -> list[dict]:
        """Normalise the bounded follow-up context.

        Malformed entries are ignored rather than rejecting the whole request:
        the question itself is still answerable without context.
        """
        raw = self.cleaned_data.get("history")
        if not isinstance(raw, list):
            return []
        entries: list[dict] = []
        for item in raw[-MAX_HISTORY_ENTRIES:]:
            if not isinstance(item, dict):
                continue
            question = item.get("question")
            if not isinstance(question, str):
                continue
            question = " ".join(question.split())[:MAX_HISTORY_QUESTION_CHARS]
            if len(question) < 2:
                continue
            intent = item.get("intent")
            entries.append(
                {
                    "question": question,
                    "intent": intent[:40] if isinstance(intent, str) else "",
                }
            )
        return entries
