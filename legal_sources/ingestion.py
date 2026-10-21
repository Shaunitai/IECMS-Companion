"""Ingestion of authorised local files and approved public URLs.

Rules enforced here:

- Only ``http``/``https`` URLs may be fetched.
- Only hosts on an explicit allow-list may be fetched.
- ``robots.txt`` is respected; a refusal is reported, not worked around.
- Authentication, CAPTCHAs and paywalls are never bypassed.
- Downloads are size-capped and time-limited, with a minimum delay between
  requests to the same host.

The ingestor records what it actually retrieved. When a page cannot be read it
records an ``inaccessible`` source with the real reason and **never** invents
contents.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .extraction import ExtractedDocument, extract
from .indexing import KeywordIndex
from .metadata import SourceMetadata, VerificationStatus, load_registry, save_registry
from .paths import CorpusPaths

DEFAULT_USER_AGENT = (
    "IECMS-Companion-Prototype/0.1 "
    "(+educational prototype; contact the project maintainer)"
)
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MIN_INTERVAL = 1.0

_CONTENT_SUFFIXES = {
    "text/html": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


class SourceIngestionError(RuntimeError):
    """Raised when a source cannot be ingested as requested."""


@dataclass
class FetchResult:
    """Outcome of an approved URL fetch."""

    url: str
    status: int
    content_type: str
    body: bytes = b""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


class SourceIngestor:
    """Ingest documents into the corpus and keep the registry up to date."""

    def __init__(
        self,
        paths: CorpusPaths,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        allowed_hosts: list[str] | None = None,
        respect_robots: bool = True,
    ) -> None:
        self.paths = paths
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.min_interval = min_interval
        self.allowed_hosts = {host.lower() for host in (allowed_hosts or [])}
        self.respect_robots = respect_robots
        self._last_request_at: dict[str, float] = {}

    # -- Registry ---------------------------------------------------------

    @property
    def registry(self) -> dict[str, SourceMetadata]:
        return load_registry(self.paths.registry)

    def _record(self, metadata: SourceMetadata) -> SourceMetadata:
        registry = self.registry
        registry[metadata.source_id] = metadata
        save_registry(self.paths.registry, registry)
        return metadata

    def _persist(self, metadata: SourceMetadata, document: ExtractedDocument) -> SourceMetadata:
        """Merge extraction warnings into the metadata and store everything."""
        warnings = tuple(dict.fromkeys([*metadata.extraction_warnings, *document.warnings]))
        final = SourceMetadata(**{**metadata.to_dict(), "extraction_warnings": warnings})
        self.paths.ensure_directories()
        document.save(self.paths.extracted_path(final.source_id))
        return self._record(final)

    # -- Local files ------------------------------------------------------

    def ingest_local_file(
        self,
        path: str | Path,
        metadata: SourceMetadata,
    ) -> SourceMetadata:
        """Extract and register an authorised local document."""
        path = Path(path)
        if not path.exists():
            raise SourceIngestionError(f"Local document not found: {path}")
        if not metadata.local_path:
            metadata = SourceMetadata(**{**metadata.to_dict(), "local_path": str(path)})
        document = extract(metadata.source_id, path, ocr=metadata.ocr)
        return self._persist(metadata, document)

    # -- Remote documents -------------------------------------------------

    def _robots_allow(self, url: str) -> tuple[bool, str]:
        """Check ``robots.txt`` for this URL. Returns (allowed, reason)."""
        if not self.respect_robots:
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
        if parser.can_fetch(self.user_agent, url):
            return True, "Allowed by robots.txt."
        return False, f"{robots_url} disallows fetching this URL."

    def _throttle(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last_request_at[host] = time.monotonic()

    def fetch_url(self, url: str) -> FetchResult:
        """Fetch an approved URL, respecting robots rules and the allow-list."""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise SourceIngestionError(
                f"Refusing to fetch non-HTTP(S) URL scheme: {parts.scheme!r}"
            )
        host = (parts.hostname or "").lower()
        if not host:
            raise SourceIngestionError("URL has no host component.")
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise SourceIngestionError(
                f"Host '{host}' is not on the approved source allow-list."
            )

        allowed, reason = self._robots_allow(url)
        if not allowed:
            return FetchResult(url=url, status=0, content_type="", error=reason)

        self._throttle(host)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    return FetchResult(
                        url=url,
                        status=response.status,
                        content_type=content_type,
                        error=f"Document exceeds the {self.max_bytes} byte limit.",
                    )
                return FetchResult(
                    url=url,
                    status=response.status,
                    content_type=content_type,
                    body=body,
                )
        except urllib.error.HTTPError as exc:
            return FetchResult(
                url=url,
                status=exc.code,
                content_type="",
                error=f"HTTP {exc.code} {exc.reason}",
            )
        except urllib.error.URLError as exc:
            return FetchResult(url=url, status=0, content_type="", error=f"URLError: {exc.reason!r}")
        except Exception as exc:  # noqa: BLE001 - report, never invent
            return FetchResult(
                url=url, status=0, content_type="", error=f"{type(exc).__name__}: {exc}"
            )

    def ingest_url(self, url: str, metadata: SourceMetadata) -> SourceMetadata:
        """Fetch, extract and register an approved public URL.

        A failed fetch produces an ``inaccessible`` record carrying the real
        reason. No placeholder content is ever created.
        """
        result = self.fetch_url(url)
        if not result.ok:
            failed = SourceMetadata(
                **{
                    **metadata.to_dict(),
                    "source_url": url,
                    "verification_status": VerificationStatus.INACCESSIBLE.value,
                    "extraction_warnings": (result.error or "Fetch failed.",),
                    "notes": (metadata.notes + " | Retrieval failed; contents not read.").strip(" |"),
                }
            )
            return self._record(failed)

        suffix = _CONTENT_SUFFIXES.get(result.content_type, "")
        if not suffix:
            failed = SourceMetadata(
                **{
                    **metadata.to_dict(),
                    "source_url": url,
                    "verification_status": VerificationStatus.INACCESSIBLE.value,
                    "extraction_warnings": (
                        f"Unsupported content type '{result.content_type or 'unknown'}'.",
                    ),
                }
            )
            return self._record(failed)

        self.paths.ensure_directories()
        local_path = self.paths.documents_dir / f"{metadata.source_id}{suffix}"
        local_path.write_bytes(result.body)

        stored = SourceMetadata(
            **{
                **metadata.to_dict(),
                "source_url": url,
                "local_path": str(local_path),
            }
        )
        document = extract(stored.source_id, local_path)
        return self._persist(stored, document)

    def mark_verified(self, source_id: str, *, verified_on: str | None = None) -> SourceMetadata:
        """Manually mark a source as verified. Never called automatically."""
        registry = self.registry
        if source_id not in registry:
            raise SourceIngestionError(f"Unknown source: {source_id}")
        metadata = registry[source_id]
        updated = SourceMetadata(
            **{
                **metadata.to_dict(),
                "verification_status": VerificationStatus.VERIFIED.value,
                "last_verified": verified_on or date.today().isoformat(),
            }
        )
        return self._record(updated)

    # -- Indexing ---------------------------------------------------------

    def load_documents(self) -> list[tuple[SourceMetadata, ExtractedDocument]]:
        """Load every registered source that has extracted text on disk."""
        documents: list[tuple[SourceMetadata, ExtractedDocument]] = []
        for metadata in self.registry.values():
            path = self.paths.extracted_path(metadata.source_id)
            if not path.exists():
                continue
            documents.append((metadata, ExtractedDocument.load(path)))
        return documents

    def rebuild_index(self) -> KeywordIndex:
        """Rebuild and persist the local keyword index."""
        index = KeywordIndex.build(self.load_documents())
        index.save(self.paths.index_file)
        return index
