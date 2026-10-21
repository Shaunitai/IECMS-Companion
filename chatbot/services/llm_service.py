"""Cloud LLM provider access.

Two provider families are supported, both chosen in ``config/settings.py``:

- ``gemini`` — Google Gemini via the official ``google-genai`` SDK.
- anything else (e.g. ``cerebras``) — any OpenAI-compatible
  ``/chat/completions`` endpoint (Cerebras, Groq, OpenRouter, Mistral, ...),
  reached with the standard library so no extra dependency is required.

Only the backend may talk to a provider. Credentials are read from Django
settings (which read them from the environment) and are never sent to the
browser.

Design notes:

- The SDK is imported lazily, so the application still starts and runs (in
  extractive mode) when the package or the credentials are absent.
- Retries are bounded by ``LLM_MAX_ATTEMPTS`` and only apply to genuinely
  transient failures (rate limits, server errors, timeouts).
- Prompts, sources and answers are **not** logged. Only the error class and
  provider status code are recorded.
- This module never returns an answer it did not receive from the provider. If
  the provider fails, the caller gets an exception and reports a clear error.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from django.conf import settings

from .retrieval_service import RetrievedPassage

logger = logging.getLogger(__name__)

# Provider status codes that are worth retrying.
_RETRYABLE_CODES = frozenset({408, 429, 500, 502, 503, 504})


class LLMServiceError(RuntimeError):
    """Raised when the configured provider cannot return a usable response."""

    def __init__(self, message: str, *, code: str = "provider_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LLMConfig:
    """Immutable snapshot of the provider configuration."""

    provider: str
    model: str
    timeout: int
    max_output_tokens: int
    max_attempts: int
    temperature: float
    api_key: str
    base_url: str = ""

    @property
    def has_credentials(self) -> bool:
        """Whether an API key is configured (the key itself is never logged)."""
        return bool(self.api_key)


@dataclass(frozen=True)
class LLMAnswer:
    """A generated explanation plus provenance."""

    text: str
    model: str
    provider: str
    attempts: int


def get_llm_config() -> LLMConfig:
    """Build the provider configuration from Django settings."""
    return LLMConfig(
        provider=settings.LLM_PROVIDER,
        model=settings.LLM_MODEL,
        timeout=settings.LLM_REQUEST_TIMEOUT,
        max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
        max_attempts=max(1, settings.LLM_MAX_ATTEMPTS),
        temperature=settings.LLM_TEMPERATURE,
        api_key=settings.LLM_API_KEY,
        base_url=getattr(settings, "LLM_BASE_URL", ""),
    )


def is_configured() -> bool:
    """Whether a provider call could be attempted at all."""
    return get_llm_config().has_credentials


def build_user_prompt(
    question: str,
    labelled_passages: list[tuple[str, RetrievedPassage]],
    *,
    redirect: bool = False,
    context: str = "",
) -> str:
    """Compose the user turn: the question plus the labelled sources."""
    lines = [f"QUESTION: {question}"]
    if context:
        lines.extend(["", f"CONVERSATION CONTEXT: {context}"])
    lines.extend(["", "SOURCES:"])
    for label, passage in labelled_passages:
        lines.append(f"[{label}] {passage.document_title} \u2014 {passage.issuing_authority}")
        location = []
        if passage.section_heading:
            location.append(f"section: {passage.section_heading}")
        if passage.page_number:
            location.append(f"page: {passage.page_number}")
        if location:
            lines.append("    (" + "; ".join(location) + ")")
        lines.append(f"    status: {passage.verification_status}")
        lines.append(f"    {passage.text}")
        lines.append("")

    if redirect:
        lines.append(
            "NOTE: the user is asking for help with their own situation. Explain "
            "the general concept from the sources if they cover it, then state "
            "clearly that you cannot advise on their case."
        )

    lines.append(
        "Answer using only the sources above. Cite them as [S1], [S2] and so on. "
        "If the sources do not establish an answer, say so. Use only the parts "
        "of the sources that address the QUESTION; ignore unrelated material "
        "and do not pad the answer with background."
    )
    return "\n".join(lines)


def _load_sdk():
    """Import google-genai lazily so the app runs without it."""
    try:
        import google.genai as genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise LLMServiceError(
            "The Google Gemini SDK (google-genai) is not installed, so the "
            "explanation engine is unavailable.",
            code="sdk_missing",
        ) from exc
    return genai, types


def _classify_error(exc: Exception) -> tuple[str, bool]:
    """Map a provider exception to (code, retryable)."""
    name = type(exc).__name__
    code = getattr(exc, "code", None)

    if name == "ClientError":
        if code == 429:
            return "rate_limited", True
        if code in {401, 403}:
            return "authentication_failed", False
        return "invalid_request", False
    if name == "ServerError":
        return "provider_unavailable", True
    if name in {"TimeoutException", "ReadTimeout", "ConnectTimeout", "TimeoutError"}:
        return "timeout", True
    if name == "HTTPError" and isinstance(code, int):
        return _classify_status(code)
    if name in {"URLError", "ConnectionError", "ConnectionResetError", "RemoteDisconnected"}:
        return "provider_unavailable", True
    if code in _RETRYABLE_CODES:
        return "provider_unavailable", True
    return "provider_error", False


def generate_explanation(
    *,
    question: str,
    labelled_passages: list[tuple[str, RetrievedPassage]],
    language: str,
    system_instruction: str,
    redirect: bool = False,
    context: str = "",
) -> LLMAnswer:
    """Ask the provider to explain the retrieved evidence in plain language.

    ``context`` is a short, client-supplied note about the previous question
    used to resolve a follow-up. It is never stored. Raises
    :class:`LLMServiceError` with a machine-readable ``code`` on failure.
    Never returns placeholder text.
    """
    config = get_llm_config()
    if not config.has_credentials:
        raise LLMServiceError(
            "No LLM credentials are configured, so the explanation engine is "
            "unavailable.",
            code="not_configured",
        )
    if not labelled_passages:
        raise LLMServiceError(
            "Refusing to call the provider without retrieved evidence.",
            code="no_evidence",
        )

    prompt = build_user_prompt(
        question, labelled_passages, redirect=redirect, context=context
    )

    if config.provider == "gemini":
        return _generate_gemini(config, prompt, system_instruction)
    return _generate_openai_compatible(config, prompt, system_instruction)


def generate_text(*, prompt: str, system_instruction: str) -> LLMAnswer:
    """Ask the provider for free text with no retrieved passages.

    Used by features that supply their own untrusted content (currently the
    document summariser). The caller is responsible for the prompt's safety
    instructions. Raises :class:`LLMServiceError` when no provider is
    configured or the call fails.
    """
    config = get_llm_config()
    if not config.has_credentials:
        raise LLMServiceError(
            "No LLM credentials are configured, so text generation is "
            "unavailable.",
            code="not_configured",
        )
    if config.provider == "gemini":
        return _generate_gemini(config, prompt, system_instruction)
    return _generate_openai_compatible(config, prompt, system_instruction)


def _generate_gemini(
    config: LLMConfig, prompt: str, system_instruction: str
) -> LLMAnswer:
    """Call Google Gemini through the official ``google-genai`` SDK."""
    genai, types = _load_sdk()

    try:
        client = genai.Client(
            api_key=config.api_key,
            http_options=types.HttpOptions(timeout=config.timeout * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - configuration problem
        logger.warning("LLM client could not be created: %s", type(exc).__name__)
        raise LLMServiceError(
            "The explanation engine could not be configured.",
            code="client_error",
        ) from exc

    generate_config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=config.temperature,
        max_output_tokens=config.max_output_tokens,
    )

    last_error: LLMServiceError | None = None
    for attempt in range(1, config.max_attempts + 1):
        try:
            response = client.models.generate_content(
                model=config.model,
                contents=prompt,
                config=generate_config,
            )
        except Exception as exc:  # noqa: BLE001 - classify and report
            code, retryable = _classify_error(exc)
            # Log the failure class only: never the prompt, sources or answer.
            logger.warning(
                "LLM call failed (attempt %s/%s): %s",
                attempt,
                config.max_attempts,
                type(exc).__name__,
            )
            last_error = LLMServiceError(_message_for(code), code=code)
            if not retryable or attempt >= config.max_attempts:
                raise last_error from exc
            time.sleep(min(2 ** (attempt - 1) * 0.5, 4.0))
            continue

        text = _strip_reasoning(getattr(response, "text", "") or "")
        if not text:
            # A blocked or empty response is a failure, not an answer.
            last_error = LLMServiceError(
                "The explanation engine returned an empty or blocked response.",
                code="empty_response",
            )
            if attempt >= config.max_attempts:
                raise last_error
            time.sleep(min(2 ** (attempt - 1) * 0.5, 4.0))
            continue

        return LLMAnswer(
            text=text,
            model=config.model,
            provider=config.provider,
            attempts=attempt,
        )

    raise last_error or LLMServiceError("The explanation engine failed.")


# Some models (Gemma 4 among them) wrap their chain of thought in a
# <thought>...</thought> block before the answer. That is the model's working,
# not the answer, and it must never be shown to the user.
_THOUGHT_BLOCK = re.compile(r"<thought>.*?</thought>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Remove any reasoning block the model emits alongside its answer."""
    cleaned = _THOUGHT_BLOCK.sub(" ", text)
    # An unterminated block means the answer was cut off; keep nothing from it
    # rather than presenting raw reasoning as the answer.
    opening = cleaned.lower().find("<thought>")
    if opening != -1:
        cleaned = cleaned[:opening]
    return cleaned.strip()


def _extract_chat_text(body: dict) -> str:
    """Pull the assistant message out of an OpenAI-compatible response."""
    try:
        choices = body.get("choices") or []
        message = choices[0].get("message") or {}
        content = message.get("content")
    except (AttributeError, IndexError, TypeError):  # noqa: BLE001 - report, never guess
        return ""
    return _strip_reasoning(content) if isinstance(content, str) else ""


def _generate_openai_compatible(
    config: LLMConfig, prompt: str, system_instruction: str
) -> LLMAnswer:
    """Call any OpenAI-compatible ``/chat/completions`` endpoint.

    Used for providers such as Cerebras, Groq, OpenRouter and Google's
    OpenAI-compatible endpoint (which serves the Gemma models). Only the
    standard library is used, so no extra dependency is required. This path is
    selected by setting ``LLM_PROVIDER`` to anything other than ``gemini``.
    """
    import json
    import urllib.error
    import urllib.request

    url = config.base_url.rstrip("/") + "/chat/completions"

    def build_payload(merge_system: bool) -> bytes:
        """Serialise the request.

        ``merge_system`` folds the system instruction into the user turn, which
        models such as Gemma require because they reject a ``system`` role.
        """
        if merge_system:
            messages = [
                {"role": "user", "content": f"{system_instruction}\n\n{prompt}"}
            ]
        else:
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
            ]
        return json.dumps(
            {
                "model": config.model,
                "messages": messages,
                "temperature": config.temperature,
                "max_tokens": config.max_output_tokens,
            }
        ).encode("utf-8")

    last_error: LLMServiceError | None = None
    attempt = 1
    merge_system = False
    while attempt <= config.max_attempts:
        request = urllib.request.Request(
            url,
            data=build_payload(merge_system),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key}",
                "Accept": "application/json",
                "User-Agent": "IECMS-Companion-Prototype/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=config.timeout) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and not merge_system:
                # The model probably does not accept a "system" role (Gemma).
                # Fold the instruction into the user turn and retry once,
                # without spending an attempt.
                logger.info(
                    "Provider rejected the system role (HTTP 400); retrying with "
                    "the instruction merged into the user message."
                )
                merge_system = True
                continue
            code, retryable = _classify_status(exc.code)
            # Log the status only: never the prompt, sources or answer.
            logger.warning(
                "LLM call failed (attempt %s/%s): HTTP %s",
                attempt,
                config.max_attempts,
                exc.code,
            )
            last_error = LLMServiceError(_message_for(code), code=code)
            if not retryable or attempt >= config.max_attempts:
                raise last_error from exc
            time.sleep(min(2 ** (attempt - 1) * 0.5, 4.0))
            attempt += 1
            continue
        except Exception as exc:  # noqa: BLE001 - classify and report
            code, retryable = _classify_error(exc)
            logger.warning(
                "LLM call failed (attempt %s/%s): %s",
                attempt,
                config.max_attempts,
                type(exc).__name__,
            )
            last_error = LLMServiceError(_message_for(code), code=code)
            if not retryable or attempt >= config.max_attempts:
                raise last_error from exc
            time.sleep(min(2 ** (attempt - 1) * 0.5, 4.0))
            attempt += 1
            continue

        text = _extract_chat_text(body)
        if not text:
            # A blocked or empty response is a failure, not an answer.
            last_error = LLMServiceError(
                "The explanation engine returned an empty or blocked response.",
                code="empty_response",
            )
            if attempt >= config.max_attempts:
                raise last_error
            time.sleep(min(2 ** (attempt - 1) * 0.5, 4.0))
            attempt += 1
            continue

        return LLMAnswer(
            text=text,
            model=config.model,
            provider=config.provider,
            attempts=attempt,
        )

    raise last_error or LLMServiceError("The explanation engine failed.")


def _classify_status(status: int) -> tuple[str, bool]:
    """Map an HTTP status from an OpenAI-compatible provider to (code, retryable)."""
    if status == 429:
        return "rate_limited", True
    if status in {401, 403}:
        return "authentication_failed", False
    if status in {408, 500, 502, 503, 504}:
        return "provider_unavailable", True
    return "invalid_request", False


def _message_for(code: str) -> str:
    return {
        "rate_limited": (
            "The explanation service is busy right now (provider rate limit). "
            "Please try again shortly."
        ),
        "authentication_failed": (
            "The explanation service could not authenticate with the provider. "
            "Please contact the administrator."
        ),
        "invalid_request": (
            "The provider rejected the request. Please contact the administrator."
        ),
        "provider_unavailable": (
            "The explanation service is temporarily unavailable. Please try again "
            "shortly."
        ),
        "timeout": (
            "The explanation service took too long to respond. Please try again."
        ),
    }.get(code, "The explanation service is unavailable. Please try again shortly.")

