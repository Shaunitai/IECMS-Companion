"""Text and metadata extraction from supported document formats.

Page numbers and section headings are preserved wherever the format allows, so
that a retrieved passage can be cited precisely. Optional third-party libraries
are imported lazily: a missing library produces an extraction warning instead of
breaking the application.

Nothing here fetches anything from the network.
"""

from __future__ import annotations

import json
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ocr import OCRPage, OCRUnavailable, ocr_pdf

# Documents shorter than this are still indexed, but this is the minimum length
# for a chunk to be worth storing.
MIN_BLOCK_CHARS = 40
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx"}


@dataclass(frozen=True)
class ExtractedBlock:
    """One citable unit of text."""

    block_id: str
    text: str
    page_number: int | None = None
    heading: str = ""
    order: int = 0


@dataclass
class ExtractedDocument:
    """All extracted blocks for one source, plus any warnings."""

    source_id: str
    blocks: list[ExtractedBlock] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "warnings": list(self.warnings),
            "blocks": [asdict(block) for block in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractedDocument":
        return cls(
            source_id=data["source_id"],
            warnings=list(data.get("warnings", [])),
            blocks=[
                ExtractedBlock(
                    block_id=b["block_id"],
                    text=b["text"],
                    page_number=b.get("page_number"),
                    heading=b.get("heading", ""),
                    order=b.get("order", 0),
                )
                for b in data.get("blocks", [])
            ],
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "ExtractedDocument":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_WHITESPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# Short numbered steps are meaningful content even when they fall below the
# ordinary minimum block length (e.g. "Step 1: Click Create New.").
_STEP_LIKE = re.compile(
    r"^\s*(?:step\s+)?\d{1,2}\s*[.):\-]\s+", re.IGNORECASE
)


def _normalise(text: str) -> str:
    """Collapse page layout into prose.

    A PDF text layer breaks lines where the printed page broke them. Those
    newlines are layout, not content, so they are flattened here: a stored
    block then reads as one paragraph instead of a stack of fragments.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # A replacement character marks a glyph the OCR engine could not read.
    # Keeping it makes the passage unreadable; it is not source content.
    text = text.replace("\ufffd", " ")
    # Rejoin words split across a line break by a hyphen.
    text = re.sub(r"(\w)-[ \t]*\n\s*(\w)", r"\1\2", text)
    # Drop unassigned/private-use code points emitted by broken font maps.
    text = "".join(
        char
        for char in text
        if char.isspace()
        or unicodedata.category(char) not in {"Cc", "Cf", "Cn", "Co", "Cs"}
    )
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def _looks_like_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 90:
        return False
    # Blockquotes, list items, table rows and rules are never headings.
    if line[0] in ">-*+|":
        return False

    # Ignore Markdown emphasis markers when judging the line.
    cleaned = line.strip("*_`# ").strip()
    if not cleaned or not any(char.isalpha() for char in cleaned):
        return False
    if cleaned.endswith((".", ",", ";", ":", "?", "!", ")")):
        return False

    # e.g. "12. Notice of Set Down" or "PART A - GENERAL"
    if re.match(r"^(\d+[.)]\s+|PART\s+[A-Z0-9]+\b|SCHEDULE\b)", cleaned):
        return True

    words = cleaned.split()
    return len(words) <= 8 and cleaned == cleaned.upper()


def _chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer to break on a sentence boundary.
            window = text.rfind(". ", start + size // 2, end)
            if window != -1:
                end = window + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [chunk for chunk in chunks if chunk]


def _blocks_from_paragraphs(
    paragraphs: list[tuple[str, int | None, bool]],
    source_id: str,
    *,
    min_chars: int = MIN_BLOCK_CHARS,
    headings_as_content: bool = False,
) -> list[ExtractedBlock]:
    """Turn ``(text, page_number, is_heading)`` triples into headed blocks.

    A heading names the section that follows; it is carried as the block's
    ``heading`` (citation metadata) and is deliberately **not** emitted as a
    passage of its own. Indexing headings as content let document titles and
    synthetic-fixture banners surface as if they were quoted answer text.

    OCR output is different: a short title-case line in a scanned manual is
    often a button or field label ("LOGIN", "REGISTRATION") rather than a
    section heading. For OCR sources the caller sets ``headings_as_content``
    so those lines remain searchable *and* stay available as heading metadata.
    """
    blocks: list[ExtractedBlock] = []
    heading = ""
    for text, page_number, is_heading in paragraphs:
        text = _normalise(text)
        if not text:
            continue
        is_heading_line = is_heading or _looks_like_heading(text)
        if is_heading_line:
            heading = text
            if not headings_as_content:
                continue
        for piece in _chunk(text):
            if (
                len(piece) < min_chars
                and not (headings_as_content and is_heading_line)
                and not _STEP_LIKE.match(piece)
            ):
                continue
            blocks.append(
                ExtractedBlock(
                    block_id=f"{source_id}#b{len(blocks)}",
                    text=piece,
                    page_number=page_number,
                    heading=heading,
                    order=len(blocks),
                )
            )
    return blocks


# ---------------------------------------------------------------------------
# Format-specific extractors
# ---------------------------------------------------------------------------


def _extract_plain_text(path: Path, source_id: str) -> tuple[list[tuple[str, int | None, bool]], list[str]]:
    """Extract text grouped into paragraphs (blank-line separated).

    Grouping by paragraph keeps a definition or a quoted passage together as one
    citable block instead of splitting it line by line, so a retrieved passage
    reads as a complete thought rather than a single wrapped line.
    """
    warnings: list[str] = []
    raw = path.read_text(encoding="utf-8", errors="replace")
    paragraphs: list[tuple[str, int | None, bool]] = []
    for block in _BLANK_LINES.split(raw):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if len(lines) == 1:
            md_match = _MD_HEADING.match(lines[0])
            if md_match:
                # Markdown headings are explicit, so trust them.
                paragraphs.append((md_match.group(2).strip(), None, True))
                continue
        # Join wrapped lines into one paragraph. Strip Markdown blockquote
        # markers so a quoted warning reads as prose.
        text = " ".join(line.lstrip(">").strip() for line in lines).strip()
        if text:
            paragraphs.append((text, None, False))
    if not paragraphs:
        warnings.append("No text could be extracted from the file.")
    return paragraphs, warnings


def _extract_html(path: Path, source_id: str) -> tuple[list[tuple[str, int | None, bool]], list[str]]:
    warnings: list[str] = []
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - dependency is installed
        return [], ["beautifulsoup4 is not installed; HTML could not be parsed."]

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()

    paragraphs: list[tuple[str, int | None, bool]] = []
    for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote"]):
        text = element.get_text(" ", strip=True)
        if text:
            paragraphs.append((text, None, element.name in {"h1", "h2", "h3", "h4"}))
    if not paragraphs:
        warnings.append("No readable text found in the HTML document.")
    return paragraphs, warnings


def _extract_pdf(
    path: Path,
    source_id: str,
    *,
    ocr: bool = False,
) -> tuple[list[tuple[str, int | None, bool]], list[str], bool]:
    warnings: list[str] = []
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is installed
        return [], ["pypdf is not installed; PDF could not be read."], False

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 - report, never guess
        return [], [f"PDF could not be opened: {type(exc).__name__}: {exc}"], False

    if getattr(reader, "is_encrypted", False):
        warnings.append("PDF is encrypted; contents were not read.")

    paragraphs: list[tuple[str, int | None, bool]] = []
    empty_pages: list[int] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Page {index} could not be extracted: {type(exc).__name__}.")
            continue
        if not text.strip():
            empty_pages.append(index)
            continue
        for chunk in _BLANK_LINES.split(text):
            chunk = chunk.strip()
            if chunk:
                paragraphs.append((chunk, index, _looks_like_heading(chunk)))

    if not paragraphs:
        original_warning = (
            "No text layer found; the PDF may be a scanned image and would need OCR."
        )
        if not ocr:
            warnings.append(original_warning)
            return paragraphs, warnings, False

        warnings.append(original_warning)
        ocr_paragraphs, ocr_warnings = _extract_pdf_with_ocr(path)
        warnings.extend(ocr_warnings)
        if ocr_paragraphs:
            warnings.append(
                "OCR was applied to the scanned pages. OCR text may contain "
                "recognition errors; page numbers refer to the PDF pages."
            )
            return ocr_paragraphs, warnings, True
        return paragraphs, warnings, False

    if ocr:
        # A text layer exists, so OCR is unnecessary. This is recorded because
        # the manifest asked for it and a reviewer should know it was skipped.
        warnings.append("PDF already has a text layer; OCR was not needed.")
    if empty_pages:
        warnings.append(
            f"{len(empty_pages)} page(s) produced no text (possibly scanned): "
            + ", ".join(str(p) for p in empty_pages[:10])
        )
    return paragraphs, warnings, False


def _extract_pdf_with_ocr(
    path: Path,
) -> tuple[list[tuple[str, int | None, bool]], list[str]]:
    """OCR a scanned PDF and turn the recognised lines into paragraphs."""
    try:
        document = ocr_pdf(path)
    except OCRUnavailable as exc:
        return [], [str(exc)]
    except Exception as exc:  # noqa: BLE001 - report, never invent text
        return [], [f"OCR failed: {type(exc).__name__}: {exc}"]

    return _paragraphs_from_ocr_pages(document.pages), list(document.warnings)


def _paragraphs_from_ocr_pages(
    pages: tuple[OCRPage, ...],
) -> list[tuple[str, int | None, bool]]:
    """Group OCR lines into paragraphs while preserving page numbers.

    Lines are grouped by vertical proximity. A line-height change is treated as
    a heading signal only when the line is short, has no sentence punctuation
    and is visibly taller than the page's median body line. This keeps OCR
    noise from turning ordinary prose into headings.

    Running headers/footers are detected across the whole document: a short
    edge-of-page line that repeats on many pages is furniture, not content.
    """
    furniture = _repeated_ocr_furniture(pages)
    paragraphs: list[tuple[str, int | None, bool]] = []
    for page in pages:
        lines = sorted(
            (line for line in page.lines if line.text.strip()),
            key=lambda line: (round(line.y, 1), line.x),
        )
        lines = [
            line
            for line in lines
            if not _is_page_furniture(line, page, furniture)
        ]
        if not lines:
            continue

        heights = [line.height for line in lines if line.height > 0]
        median_height = statistics.median(heights) if heights else 12.0

        # A dictionary or gazette page is laid out in columns. Sorting the
        # recognised lines by vertical position alone interleaves the columns
        # and destroys the reading order, so each column is grouped separately.
        for column in _split_ocr_columns(lines, page.width):
            for group in _group_ocr_lines(column, median_height):
                text = _join_ocr_lines(group)
                if not text:
                    continue
                paragraphs.append(
                    (
                        text,
                        page.page_number,
                        _looks_like_ocr_heading(group, text, median_height),
                    )
                )
    return paragraphs


def _split_ocr_columns(lines, page_width: float) -> list[list]:
    """Split a page's lines into reading-order columns.

    The split is conservative: it is only applied when the page has a clear
    vertical gutter at its centre, with substantial text on both sides and
    few lines crossing it. Single-column pages (most statutes and manuals) are
    returned unchanged.
    """
    if page_width <= 0 or len(lines) < 8:
        return [lines]

    centre = page_width / 2
    left = [
        line
        for line in lines
        if line.width > 0 and line.x + line.width <= centre
    ]
    right = [line for line in lines if line.width > 0 and line.x >= centre]
    crossing = [line for line in lines if line not in left and line not in right]

    # A genuine two-column page has most of its lines on one side or the other.
    if len(left) < 4 or len(right) < 4:
        return [lines]
    if len(crossing) > max(2, len(lines) // 5):
        return [lines]

    key = lambda line: (round(line.y, 1), line.x)
    columns = [sorted(left, key=key), sorted(right, key=key)]
    if crossing:
        # A running header spans the gutter and belongs before the columns.
        columns.insert(0, sorted(crossing, key=key))
    return columns


def _group_ocr_lines(lines, median_height: float) -> list[list]:
    """Group one column of OCR lines into paragraphs by vertical proximity."""
    groups: list[list] = []
    current: list = []
    previous = None
    for line in lines:
        if current and previous is not None:
            gap = line.y - previous.bottom
            new_column = (
                line.x + 40 < current[0].x
                and line.y <= previous.y + median_height
            )
            # A visibly taller, short line is a heading. Flush it before
            # the body text that follows, even when the vertical gap is
            # small (headings are often close to the next line).
            heading_start = (
                line.height >= median_height * 1.25
                and len(line.text.strip()) <= 80
                and not line.text.strip().endswith((".", ":", ";", "?", "!"))
            )
            previous_heading = (
                previous.height >= median_height * 1.25
                and len(previous.text.strip()) <= 80
                and not previous.text.strip().endswith((".", ":", ";", "?", "!"))
            )
            if (
                gap > max(median_height * 0.9, 8.0)
                or new_column
                or heading_start
                or previous_heading
            ):
                groups.append(current)
                current = []
        current.append(line)
        previous = line
    if current:
        groups.append(current)
    return groups


def _edge_line(line, page: OCRPage) -> bool:
    if not page.height:
        return False
    return line.y < page.height * 0.07 or line.bottom > page.height * 0.93


def _furniture_key(text: str) -> str:
    """A digit- and punctuation-insensitive key for repeated edge lines."""
    key = re.sub(r"[\W\d_]+", " ", text.lower())
    return " ".join(key.split())


def _repeated_ocr_furniture(pages: tuple[OCRPage, ...]) -> frozenset[str]:
    """Find running headers, footers, logos and repeated UI labels.

    OCR output often repeats a chapter header or a logo on many pages. Such a
    line is short, has no sentence punctuation, and appears across several
    pages. Counting those lines across the whole document is more reliable than
    relying only on their position, because scanned pages are not perfectly
    aligned.
    """
    counts: Counter[str] = Counter()
    total = 0
    for page in pages:
        total += 1
        seen: set[str] = set()
        for line in page.lines:
            text = line.text.strip()
            if not text or len(text) > 80:
                continue
            if text.endswith((".", ":", ";", "?", "!")):
                continue
            key = _furniture_key(text)
            if key and key not in seen:
                counts[key] += 1
                seen.add(key)
    if total < 3:
        return frozenset()
    threshold = max(3, int(total * 0.1))
    return frozenset(key for key, count in counts.items() if count >= threshold)


def _is_page_furniture(
    line, page: OCRPage, repeated: frozenset[str]
) -> bool:
    """Drop obvious page numbers and running headers/footers before chunking."""
    text = line.text.strip()
    if not text:
        return True
    if _furniture_key(text) in repeated:
        # A short, unpunctuated line repeated across the document is running
        # furniture or a repeated label, not a passage of the manual.
        return True
    if not _edge_line(line, page):
        return False
    if re.fullmatch(r"[\W\d_]{0,8}", text):
        return True
    return bool(re.search(r"\bpage\b", text, re.IGNORECASE))


def _looks_like_ocr_heading(group, text: str, median_height: float) -> bool:
    """Decide whether an OCR paragraph is a heading rather than body text."""
    if len(group) > 2 or len(text) > 100:
        return False
    if text.endswith((".", ":", ";", "?", "!")):
        return False
    words = text.split()
    if not words or len(words) > 9:
        return False
    tallest = max((line.height for line in group), default=0.0)
    title_case = all(
        word[:1].isupper() for word in words if word[:1].isalpha()
    )
    return tallest >= median_height * 1.12 or (title_case and len(words) <= 6)


def _join_ocr_lines(lines) -> str:
    """Join wrapped OCR lines, repairing a trailing hyphen where possible."""
    parts: list[str] = []
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        if parts:
            if parts[-1].endswith("-") and text[:1].islower():
                parts[-1] = parts[-1][:-1] + text
                continue
            if text[:1] in ".,;:)]%":
                parts[-1] = parts[-1] + text
                continue
        parts.append(text)
    return _normalise(" ".join(parts))


def _extract_docx(path: Path, source_id: str) -> tuple[list[tuple[str, int | None, bool]], list[str]]:
    warnings: list[str] = []
    try:
        import docx  # python-docx
    except ImportError:  # pragma: no cover - dependency is installed
        return [], ["python-docx is not installed; DOCX could not be read."]

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        return [], [f"DOCX could not be opened: {type(exc).__name__}: {exc}"]

    paragraphs: list[tuple[str, int | None, bool]] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            style = (getattr(paragraph.style, "name", "") or "").lower()
            paragraphs.append((text, None, style.startswith("heading")))
    if not paragraphs:
        warnings.append("No text found in the DOCX body (tables are not extracted).")
    return paragraphs, warnings


_EXTRACTORS = {
    ".txt": _extract_plain_text,
    ".md": _extract_plain_text,
    ".markdown": _extract_plain_text,
    ".html": _extract_html,
    ".htm": _extract_html,
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract(
    source_id: str,
    path: str | Path,
    *,
    ocr: bool = False,
) -> ExtractedDocument:
    """Extract blocks and warnings from a local document.

    Raises :class:`FileNotFoundError` for a missing file and
    :class:`ValueError` for an unsupported format. Extraction failures are
    reported through ``warnings`` rather than raised, so a partially readable
    document can still be inspected and reviewed.

    ``ocr`` is only meaningful for PDFs. It asks the extractor to fall back to
    OCR when the PDF has no text layer. It never adds text that OCR did not
    recognise.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such document: {path}")

    suffix = path.suffix.lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"Unsupported document type '{suffix}'. Supported: {supported}")

    used_ocr = False
    if suffix == ".pdf":
        paragraphs, warnings, used_ocr = _extract_pdf(path, source_id, ocr=ocr)
    else:
        paragraphs, warnings = extractor(path, source_id)
    blocks = _blocks_from_paragraphs(
        paragraphs,
        source_id,
        # OCR output is noisier and its short lines are often UI labels, so
        # keep them rather than discarding them as sub-40-character fragments.
        min_chars=12 if used_ocr else MIN_BLOCK_CHARS,
        headings_as_content=used_ocr,
    )
    if not blocks and not warnings:
        warnings.append("Extraction produced no usable blocks.")
    return ExtractedDocument(source_id=source_id, blocks=blocks, warnings=warnings)
