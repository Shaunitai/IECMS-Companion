"""Open-web fallback search, used only when the approved corpus cannot answer.

This module is deliberately quarantined from the approved-source pipeline:

- it is off unless ``ENABLE_WEB_FALLBACK`` is true;
- the caller only consults it *after* retrieval has reported that the approved
  corpus does not establish an answer;
- every page it returns is labelled an **unverified web source** with its URL
  and retrieval date, and is never presented as official law;
- fetched pages are treated as untrusted data, exactly like ingested documents,
  so any instruction embedded in a page is ignored;
- ``robots.txt`` is honoured before every fetch and requests are throttled.

Search uses the DuckDuckGo HTML endpoint (``html.duckduckgo.com``), which its
``robots.txt`` permits. Note the trade-off honestly: the official DuckDuckGo
instant-answer API **disallows** robots access, and scraping any search engine
is a terms-of-service grey area. A keyed provider (Brave, Tavily, Google) can
replace :func:`_search_duckduckgo` without touching anything else.
"""

from __future__ import annotations

import html
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from django.conf import settings

from legal_sources.extraction import extract
from legal_sources.indexing import query_terms, term_in_text

from .retrieval_service import RetrievedPassage

SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"

_RESULT = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

_ADDRESSABLE_SCHEMES = {"http", "https"}

# Content types we can turn into citable text.
_SUFFIXES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
}

WEB_STATUS = "web_unverified"
WEB_TIER_LABEL = "Unverified web source"


class WebSearchError(RuntimeError):
    """Raised when the fallback search itself cannot be performed."""


@dataclass(frozen=True)
class WebSource:
    """One open-web page offered as a fallback source."""

    title: str
    url: str
    host: str
    snippet: str = ""
    passages: tuple[str, ...] = ()
    retrieved_on: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.passages)


@dataclass(frozen=True)
class WebSearchOutcome:
    """The result of one fallback search, including why pages were dropped."""

    query: str
    sources: tuple[WebSource, ...] = ()
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> tuple[WebSource, ...]:
        return tuple(source for source in self.sources if source.ok)

    @property
    def has_evidence(self) -> bool:
        return bool(self.usable)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean(markup: str) -> str:
    """Strip tags, unescape entities and collapse whitespace."""
    return _WHITESPACE.sub(" ", html.unescape(_TAGS.sub(" ", markup))).strip()


def _decode_result_url(href: str) -> str:
    """Unwrap DuckDuckGo's ``/l/?uddg=`` redirect into the real target URL."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parts.query).get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return href


def _robots_allow(url: str, user_agent: str) -> tuple[bool, str]:
    """Check ``robots.txt`` for ``url``. Returns ``(allowed, reason)``."""
    if not getattr(settings, "SOURCE_RESPECT_ROBOTS", True):
        return True, "robots.txt checking disabled by configuration."
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except Exception as exc:  # noqa: BLE001 - report, do not guess
        return False, (
            f"Could not read {robots_url} ({type(exc).__name__}); refusing to "
            "fetch because the site's crawling rules could not be established."
        )
    if parser.can_fetch(user_agent, url):
        return True, "Allowed by robots.txt."
    return False, f"{robots_url} disallows fetching this URL."


class _Throttle:
    """Minimum delay between requests to the same host."""

    def __init__(self, interval: float) -> None:
        self.interval = max(interval, 0.0)
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
        self._last[host] = time.monotonic()


def _agent() -> str:
    return getattr(settings, "SOURCE_USER_AGENT", "IECMS-Companion-Prototype/0.1")


def _timeout() -> int:
    return int(getattr(settings, "WEB_SEARCH_TIMEOUT", 20))


def _max_bytes() -> int:
    return int(getattr(settings, "WEB_FETCH_MAX_BYTES", 2 * 1024 * 1024))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _search_duckduckgo(query: str, max_results: int) -> tuple[list[tuple[str, str, str]], str]:
    """Return ``[(title, url, snippet)]`` plus a diagnostic string."""
    allowed, reason = _robots_allow(SEARCH_ENDPOINT, _agent())
    if not allowed:
        return [], reason

    payload = urllib.parse.urlencode({"q": query, "kl": "wt-wt"}).encode("utf-8")
    request = urllib.request.Request(
        SEARCH_ENDPOINT,
        data=payload,
        headers={
            "User-Agent": _agent(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            body = response.read(_max_bytes()).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return [], f"Search request failed: HTTP {exc.code}."
    except Exception as exc:  # noqa: BLE001 - report, never guess
        return [], f"Search request failed: {type(exc).__name__}."

    snippets = [_clean(match) for match in _SNIPPET.findall(body)]
    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for index, (href, title_markup) in enumerate(_RESULT.findall(body)):
        url = _decode_result_url(href)
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in _ADDRESSABLE_SCHEMES or url in seen:
            continue
        seen.add(url)
        snippet = snippets[index] if index < len(snippets) else ""
        results.append((_clean(title_markup) or url, url, snippet))
        if len(results) >= max_results:
            break

    if not results:
        return [], "The search returned no usable results."
    return results, ""


# ---------------------------------------------------------------------------
# Fetch and extract
# ---------------------------------------------------------------------------


def _fetch_passages(url: str, source_id: str) -> tuple[list[str], str]:
    """Fetch ``url`` and extract citable passages. Returns ``(passages, error)``."""
    allowed, reason = _robots_allow(url, _agent())
    if not allowed:
        return [], reason

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _agent(),
            "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            body = response.read(_max_bytes() + 1)
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}."
    except Exception as exc:  # noqa: BLE001 - report, never guess
        return [], f"{type(exc).__name__}."

    if len(body) > _max_bytes():
        return [], "Document exceeds the configured size limit."
    suffix = _SUFFIXES.get(content_type)
    if not suffix:
        return [], f"Unsupported content type '{content_type or 'unknown'}'."

    limit = int(getattr(settings, "WEB_FETCH_MAX_PASSAGES", 4))
    # Reuse the tested extraction pipeline rather than parsing HTML here.
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(body)
        temp_path = Path(handle.name)
    try:
        document = extract(source_id, temp_path)
    except Exception as exc:  # noqa: BLE001 - report, never guess
        return [], f"Could not read the document: {type(exc).__name__}."
    finally:
        temp_path.unlink(missing_ok=True)

    passages = [block.text.strip() for block in document.blocks if block.text.strip()]
    if not passages:
        detail = document.warnings[0] if document.warnings else "no readable text"
        return [], detail
    return passages[:limit], ""


def search_and_fetch(query: str, *, max_results: int | None = None) -> WebSearchOutcome:
    """Search the open web and fetch a few pages as unverified evidence.

    Never raises for ordinary failures: a page that cannot be read is reported
    in its own entry with the real reason and contributes no passages.
    """
    limit = max_results or int(getattr(settings, "WEB_SEARCH_MAX_RESULTS", 4))
    limit = max(1, min(limit, 10))
    diagnostics: list[str] = []

    results, problem = _search_duckduckgo(query, limit)
    if problem:
        diagnostics.append(problem)
        return WebSearchOutcome(query=query, diagnostics=tuple(diagnostics))

    throttle = _Throttle(float(getattr(settings, "SOURCE_FETCH_MIN_INTERVAL", 1.0)))
    today = date.today().isoformat()
    sources: list[WebSource] = []
    for index, (title, url, snippet) in enumerate(results, start=1):
        host = urllib.parse.urlsplit(url).hostname or ""
        throttle.wait(host)
        passages, error = _fetch_passages(url, f"web-{index}")
        if error:
            diagnostics.append(f"Dropped {host}: {error}")
        sources.append(
            WebSource(
                title=title,
                url=url,
                host=host,
                snippet=snippet,
                passages=tuple(passages),
                retrieved_on=today,
                error=error,
            )
        )

    return WebSearchOutcome(query=query, sources=tuple(sources), diagnostics=tuple(diagnostics))


def build_web_passages(outcome: WebSearchOutcome, *, max_chars: int = 1800) -> list[RetrievedPassage]:
    """Convert usable web pages into passages the existing pipeline can cite.

    Only excerpts that mention a meaningful term from the question are kept.
    A page that never mentions the subject (a navigation page, an unrelated
    article, a generic homepage) is dropped rather than handed to the model,
    so the answer stays aligned with what the user asked. One page becomes
    one passage, so a citation points at a URL the reader can check. Every
    passage is explicitly marked unverified and is never treated as an
    approved official source.
    """
    terms = query_terms(outcome.query)
    passages: list[RetrievedPassage] = []
    for source in outcome.usable:
        text = _page_excerpts(source, terms, max_chars=max_chars)
        if not text:
            # Nothing on this page mentions the question's subject.
            continue
        passages.append(
            RetrievedPassage(
                passage_id=f"web-{len(passages) + 1}",
                text=text,
                source_id=f"web:{source.host}",
                document_title=source.title,
                issuing_authority=source.host,
                document_type="Web page (unverified)",
                tier_label=WEB_TIER_LABEL,
                verification_status=WEB_STATUS,
                disclosure=(
                    f"UNVERIFIED WEB SOURCE retrieved {source.retrieved_on} from "
                    f"{source.host}. This is not an approved official source and "
                    "has not been verified against any instrument."
                ),
                source_url=source.url,
            )
        )
    return passages


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _matches(term: str, text: str) -> bool:
    return term_in_text(term, text.lower(), prefix_length=4)


def _relevant_excerpt(
    text: str, terms: list[str], *, max_chars: int = 900
) -> str:
    """Return only the sentences of ``text`` that mention the question."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    if not terms:
        return cleaned[:max_chars]

    sentences = _SENTENCE.split(cleaned)
    selected = [
        sentence
        for sentence in sentences
        if any(_matches(term, sentence) for term in terms)
    ]
    if not selected:
        return ""
    excerpt = " ".join(selected)
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
    return excerpt


def _page_excerpts(
    source: WebSource, terms: list[str], *, max_chars: int = 1800
) -> str:
    """Collect query-aligned excerpts from one page, snippet first."""
    pieces: list[str] = []
    snippet = _relevant_excerpt(source.snippet, terms, max_chars=400)
    if snippet:
        pieces.append(snippet)
    for passage in source.passages:
        piece = _relevant_excerpt(passage, terms, max_chars=700)
        if piece and piece not in pieces:
            pieces.append(piece)
        if sum(len(item) for item in pieces) >= max_chars:
            break
    return " ".join(pieces)[:max_chars].strip()
