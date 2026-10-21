"""Extract ordered steps from manual or generated text.

The interface can render a numbered list only when the text genuinely contains
numbered steps. This module is deliberately conservative: it requires at least
two sequential markers, so an ordinary sentence containing a number is not
turned into a procedure.
"""

from __future__ import annotations

import re

# Matches an explicit "Step 1: " anywhere, or a bare "1. " / "1) " at the
# start of a line. It deliberately does NOT match "Figure 16:" or "Section
# 12.1": those are captions or references, not steps.
_MARKER = re.compile(
    r"\bstep\s+(\d{1,2})\s*[.):\-]\s+"
    r"|(?:^|\n)\s*(\d{1,2})\s*[.)]\s+",
    re.IGNORECASE | re.MULTILINE,
)

MAX_STEPS = 20
MAX_STEP_CHARS = 600

_STEP_PASSAGE = re.compile(
    r"^\s*(?:step\s+)?(\d{1,2})\s*[.):\-]\s+", re.IGNORECASE
)


def extract_steps_from_passages(
    passages,
    *,
    max_steps: int = MAX_STEPS,
) -> list[str]:
    """Collect numbered steps from retrieved passages in reading order.

    This is used by the extractive manual path. Working from whole passages
    avoids pulling a figure caption or the next section's heading into the
    body of a step, which is what happens when OCR blocks are naively joined.
    """
    steps: list[str] = []
    expected = 0
    current: str | None = None

    def flush() -> None:
        nonlocal current
        if current:
            if len(current) > MAX_STEP_CHARS:
                current = current[:MAX_STEP_CHARS].rsplit(" ", 1)[0].strip()
            steps.append(current)
            current = None

    for passage in passages:
        text = " ".join((getattr(passage, "text", "") or "").split())
        if not text:
            continue
        match = _STEP_PASSAGE.match(text)
        if match:
            number = int(match.group(1))
            if expected == 0 and number != 1:
                # Wait for the procedure to start at Step 1.
                continue
            if expected and number <= expected:
                break
            if expected and number > expected + 3:
                break
            flush()
            # The UI renders an ordered list, so drop the manual's own
            # "Step N:" prefix to avoid "1. Step 1: ...".
            current = _STEP_PASSAGE.sub("", text, count=1).strip()
            expected = number
            if len(steps) >= max_steps:
                break
            continue

        # A lower-case continuation of the current step (OCR often splits one
        # sentence across two blocks). Do not absorb headings or figure text.
        if (
            current
            and len(current) + len(text) <= MAX_STEP_CHARS
            and len(text) <= 180
            and text[:1].islower()
            and not current.endswith((".", "!", "?"))
            and not text.lower().startswith(("figure ", "table "))
        ):
            current = f"{current} {text}"

    flush()
    return steps if len(steps) >= 2 else []


def extract_steps(text: str, *, max_steps: int = MAX_STEPS) -> list[str]:
    """Return the numbered steps in ``text`` in order, or an empty list.

    A list is returned only when the numbering is sequential from 1 (gaps are
    tolerated only when the missing number still appears later, which is common
    when OCR splits a step across a page).
    """
    if not text or not text.strip():
        return []

    matches = list(_MARKER.finditer(text))
    if len(matches) < 2:
        return []

    numbered: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        number = int(match.group(1) or match.group(2))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = " ".join(text[start:end].split())
        if not body:
            continue
        if len(body) > MAX_STEP_CHARS:
            body = body[:MAX_STEP_CHARS].rsplit(" ", 1)[0].strip()
        numbered.append((number, body))

    if len(numbered) < 2:
        return []

    # Require the first marker to be 1. When the numbering restarts, a new
    # procedure begins; keep the first sequence and ignore the rest.
    if numbered[0][0] != 1:
        return []
    previous = 0
    sequence: list[tuple[int, str]] = []
    for number, body in numbered:
        if sequence and number <= previous:
            break
        if number > previous + 3:
            break
        sequence.append((number, body))
        previous = number

    # De-duplicate markers that OCR repeated for the same step.
    steps: list[str] = []
    seen: set[str] = set()
    for _, body in sequence:
        key = body.lower()
        if key in seen:
            continue
        seen.add(key)
        steps.append(body)
        if len(steps) >= max_steps:
            break
    return steps if len(steps) >= 2 else []
