"""HTTP views for the IECMS Companion chat interface.

Views stay thin: they validate transport-level concerns (JSON shape, method,
CSRF) and delegate the actual work to the service layer.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import tempfile
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from legal_sources.extraction import extract
from legal_sources.metadata import load_registry
from legal_sources.paths import CorpusPaths

from .constants import DISCLAIMER_TEXT, EXAMPLE_QUESTIONS, MOCK_MODE_NOTICE, PRIVACY_REMINDER
from .forms import ChatRequestForm
from .services import explanation_service, llm_service, summarisation_service

logger = logging.getLogger(__name__)


@require_GET
def index(request):
    """Render the chat interface shell."""
    context = {
        "example_questions": EXAMPLE_QUESTIONS,
        "disclaimer": DISCLAIMER_TEXT,
        "privacy_reminder": PRIVACY_REMINDER,
        "mock_mode": explanation_service.is_mock_mode(),
        "mock_notice": MOCK_MODE_NOTICE,
        "max_question_length": settings.CHAT_MAX_QUESTION_LENGTH,
        "document_summary_enabled": settings.ENABLE_DOCUMENT_SUMMARY,
        "max_upload_bytes": settings.DOCUMENT_SUMMARY_MAX_BYTES,
        "max_upload_mb": settings.DOCUMENT_SUMMARY_MAX_BYTES // (1024 * 1024),
    }
    return render(request, "chatbot/index.html", context)


def _error(message: str, *, code: str, status: int, field: str | None = None):
    """Build a consistent JSON error payload."""
    error: dict[str, str] = {"code": code, "message": message}
    if field:
        error["field"] = field
    return JsonResponse({"status": "error", "error": error}, status=status)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@require_GET
def source_document(request, source_id: str):
    """Serve a stored source document so a citation can be opened.

    Only files registered in the corpus and stored under its documents or
    synthetic directory are served. The registry is the authority for *what*
    may be opened; the path check prevents a malformed registry entry from
    exposing anything else on the filesystem.
    """
    paths = CorpusPaths.default(settings.LEGAL_SOURCES_DIR)
    metadata = load_registry(paths.registry).get(source_id)
    if metadata is None or not metadata.local_path:
        raise Http404("Unknown source")

    try:
        document = Path(metadata.local_path).resolve()
    except OSError as exc:  # noqa: BLE001 - never leak filesystem details
        raise Http404("Source file is not available") from exc

    allowed = (
        paths.documents_dir.resolve(),
        paths.synthetic_dir.resolve(),
    )
    if not any(_is_within(document, root) for root in allowed):
        raise Http404("Source file is outside the corpus")
    if not document.is_file():
        raise Http404("Source file is not available")

    content_type, _ = mimetypes.guess_type(document.name)
    response = FileResponse(
        document.open("rb"),
        content_type=content_type or "application/octet-stream",
    )
    safe_name = document.name.replace('"', "").replace("\r", "").replace("\n", "")
    response["Content-Disposition"] = f'inline; filename="{safe_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_POST
@csrf_protect
def chat(request):
    """Handle a single terminology question.

    Phase 1 returns a clearly-labelled mock answer. No user message is stored
    and no message content is written to the logs.
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return _error(
            "The request body must be valid JSON.",
            code="invalid_json",
            status=400,
        )

    if not isinstance(payload, dict):
        return _error(
            "The request body must be a JSON object.",
            code="invalid_payload",
            status=400,
        )

    form = ChatRequestForm(payload)
    if not form.is_valid():
        field_name, field_errors = next(iter(form.errors.items()))
        return _error(
            field_errors[0],
            code="invalid_input",
            status=400,
            field=field_name,
        )

    if explanation_service.is_mock_mode():
        result = explanation_service.build_mock_response(
            question=form.cleaned_data["question"],
        )
    else:
        try:
            result = explanation_service.answer_question(
                question=form.cleaned_data["question"],
                history=form.cleaned_data.get("history") or [],
            )
        except Exception:  # noqa: BLE001 - never leak internals to the client
            logger.exception("Unhandled failure in the explanation pipeline")
            return _error(
                "The explanation service encountered an unexpected problem. "
                "Please try again shortly.",
                code="internal_error",
                status=500,
            )

    return JsonResponse(result, status=200)


# The summary is generated from the user's own file, not from the approved
# corpus. It is labelled as such and never presented as an official source.
SUMMARY_NOTICE = (
    "Generated from the document you uploaded. It is not an approved source; "
    "check it against the original. The file was processed temporarily and "
    "deleted."
)
SUMMARY_UNAVAILABLE_TEXT = (
    "I cannot summarise documents right now because the language model is not "
    "configured. No summary has been produced, and I will not guess from the "
    "first sentences."
)


def _summary_payload(status: str, **fields):
    payload = {"status": status, "label": "DOCUMENT SUMMARY"}
    payload.update(fields)
    return JsonResponse(payload, status=200)


@require_POST
@csrf_protect
def summarise_document(request):
    """Summarise an uploaded PDF/DOCX/text file and delete it immediately.

    The file is written to a temporary path only so the existing extraction
    code can read it. It is removed in ``finally`` and is never stored in the
    knowledge base, the database or the logs.
    """
    if not settings.ENABLE_DOCUMENT_SUMMARY:
        return _error(
            "Document summarisation is disabled.",
            code="disabled",
            status=503,
        )

    upload = request.FILES.get("document")
    if upload is None:
        return _error(
            "Choose a document to summarise.",
            code="missing_file",
            status=400,
            field="document",
        )

    name = Path(upload.name or "").name
    suffix = Path(name).suffix.lower()
    allowed = tuple(settings.DOCUMENT_SUMMARY_ALLOWED_EXTENSIONS)
    if suffix not in allowed:
        return _error(
            "Unsupported file type. Upload a PDF, Word, text, Markdown or "
            "HTML file.",
            code="unsupported_type",
            status=400,
            field="document",
        )

    if upload.size <= 0:
        return _error(
            "The uploaded file is empty.",
            code="empty_file",
            status=400,
            field="document",
        )
    if upload.size > settings.DOCUMENT_SUMMARY_MAX_BYTES:
        megabytes = settings.DOCUMENT_SUMMARY_MAX_BYTES // (1024 * 1024)
        return _error(
            f"The file is larger than {megabytes} MB.",
            code="too_large",
            status=400,
            field="document",
        )

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            temp_path = Path(temp.name)
            for chunk in upload.chunks():
                temp.write(chunk)

        try:
            document = extract("uploaded-document", temp_path, ocr=False)
        except (ValueError, OSError, FileNotFoundError):
            return _error(
                "The file could not be read.",
                code="unreadable",
                status=400,
                field="document",
            )

        text = "\n\n".join(block.text for block in document.blocks).strip()
        if not text:
            detail = " ".join(document.warnings).strip()
            message = (
                "No readable text was found in that file. A scanned PDF needs "
                "a text layer before it can be summarised."
            )
            if detail:
                message = f"{message} {detail}"
            return _summary_payload("no_text", message=message)

        if not summarisation_service.is_available():
            return _summary_payload(
                "summary_unavailable", message=SUMMARY_UNAVAILABLE_TEXT
            )

        try:
            result = summarisation_service.summarise_document(
                text, filename=name
            )
        except summarisation_service.SummaryUnavailable:
            return _summary_payload(
                "summary_unavailable", message=SUMMARY_UNAVAILABLE_TEXT
            )
        except llm_service.LLMServiceError as exc:
            logger.warning("Document summary provider failed: code=%s", exc.code)
            return _summary_payload(
                "provider_error",
                message=(
                    "The summary service is temporarily unavailable. "
                    "Please try again shortly."
                ),
            )

        return _summary_payload(
            "ok",
            summary=result.text,
            filename=name[:120],
            truncated=result.truncated,
            characters=result.characters,
            model=result.model,
            provider=result.provider,
            notice=SUMMARY_NOTICE,
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                logger.warning("A temporary upload could not be deleted.")
