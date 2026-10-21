"""Optional OCR for scanned PDF sources.

Some approved documents are scans with no text layer (the IECMS manual is one).
This module renders PDF pages and runs the operating system's OCR engine.

The implementation is deliberately isolated from the rest of the extraction
code:

- dependencies are imported lazily, so the application still starts and the
  existing text-based extraction still works when OCR is unavailable;
- OCR is opt-in per source (``SourceMetadata.ocr``), so a scanned statute is
  not silently OCR'd just because it is ingested;
- failures are reported as warnings, never as invented text.

The bundled implementation uses ``pymupdf`` for rendering and ``winocr`` for the
Windows OCR engine. On a machine without those optional packages (or without a
Windows OCR language pack) ``ocr_pdf`` raises :class:`OCRUnavailable` and the
extractor records a clear warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DPI = 200
DEFAULT_LANGUAGE = "en-US"


class OCRUnavailable(RuntimeError):
    """Raised when no usable OCR engine can be loaded."""


@dataclass(frozen=True)
class OCRLine:
    """One recognised line and its position on the rendered page."""

    text: str
    x: float
    y: float
    width: float
    height: float

    @property
    def bottom(self) -> float:
        return self.y + self.height


@dataclass(frozen=True)
class OCRPage:
    """All recognised lines for one PDF page."""

    page_number: int
    width: float
    height: float
    lines: tuple[OCRLine, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OCRDocument:
    """OCR output for a whole PDF, including per-page failures."""

    pages: tuple[OCRPage, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.pages


def _load_dependencies():
    """Import the optional OCR stack, raising a clear error when absent."""
    try:
        import pymupdf  # type: ignore[import-not-found]
        import winocr  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise OCRUnavailable(
            "OCR is not available: install the optional 'pymupdf', 'winocr' "
            "and 'pillow' packages. Windows OCR also needs an installed "
            "English OCR language pack."
        ) from exc
    return pymupdf, winocr, Image


def is_available() -> bool:
    """Whether the OCR dependencies can be imported on this machine."""
    try:
        _load_dependencies()
    except OCRUnavailable:
        return False
    return True


def _field(value, name: str, default=None):
    """Read a field from either a WinRT object or a picklified dict."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _line_from_result(line) -> OCRLine | None:
    """Convert a ``winocr`` line into a plain, picklable :class:`OCRLine`.

    ``winocr.recognize_pil_sync`` returns plain dictionaries, while the async
    call returns WinRT objects. Supporting both keeps the extractor independent
    of which entry point is used.
    """
    text = (_field(line, "text", "") or "").strip()
    if not text:
        return None

    words = list(_field(line, "words", ()) or ())
    rects = [
        rect
        for rect in (_field(word, "bounding_rect") for word in words)
        if rect is not None
    ]
    if rects:
        x = min(float(_field(rect, "x", 0.0)) for rect in rects)
        y = min(float(_field(rect, "y", 0.0)) for rect in rects)
        right = max(
            float(_field(rect, "x", 0.0)) + float(_field(rect, "width", 0.0))
            for rect in rects
        )
        bottom = max(
            float(_field(rect, "y", 0.0)) + float(_field(rect, "height", 0.0))
            for rect in rects
        )
        return OCRLine(
            text=text,
            x=x,
            y=y,
            width=right - x,
            height=bottom - y,
        )

    # A line without word boxes is still useful: keep it at the origin.
    return OCRLine(text=text, x=0.0, y=0.0, width=0.0, height=0.0)


def ocr_pdf(
    path: str | Path,
    *,
    dpi: int = DEFAULT_DPI,
    language: str = DEFAULT_LANGUAGE,
) -> OCRDocument:
    """Render and OCR every page of a PDF.

    A page that cannot be rendered or recognised is skipped and reported in
    ``warnings``; the remaining pages are still returned. The caller decides
    whether a partial result is useful.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such document: {path}")

    pymupdf, winocr, Image = _load_dependencies()

    try:
        document = pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001 - report, never guess
        raise OCRUnavailable(
            f"PDF could not be opened for OCR: {type(exc).__name__}: {exc}"
        ) from exc

    pages: list[OCRPage] = []
    warnings: list[str] = []
    try:
        for page_number, page in enumerate(document, start=1):
            try:
                pixmap = page.get_pixmap(dpi=max(dpi, 100))
                image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                result = winocr.recognize_pil_sync(image, language)
                lines = tuple(
                    line
                    for line in (
                        _line_from_result(raw)
                        for raw in (_field(result, "lines", ()) or ())
                    )
                    if line is not None
                )
                pages.append(
                    OCRPage(
                        page_number=page_number,
                        width=float(pixmap.width),
                        height=float(pixmap.height),
                        lines=lines,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad page must not lose the rest
                warnings.append(
                    f"Page {page_number} could not be OCR'd: "
                    f"{type(exc).__name__}: {exc}"
                )
    finally:
        document.close()

    if not pages and not warnings:
        warnings.append("OCR produced no pages.")
    return OCRDocument(pages=tuple(pages), warnings=tuple(warnings))
