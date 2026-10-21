"""Search over the approved knowledge base.

The public contract is deliberately narrow so the implementation can change
(keyword search now; hybrid or vector retrieval later) without touching the chat
application.

Retrieval only ever returns passages from sources that are allowed to ground an
authoritative answer:

- ``verified`` sources always qualify;
- ``synthetic`` fixtures qualify only when explicitly enabled, and every answer
  built from them must disclose that they are test fixtures;
- every other status (``pending``, ``outdated``, ``superseded``, ``inaccessible``,
  ``unverified``) is excluded, because unknown currency must not be presented as
  current.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from django.conf import settings

from legal_sources.indexing import KeywordIndex, query_terms, term_in_text, tokenize
from legal_sources.metadata import SourceMetadata, load_registry
from legal_sources.paths import CorpusPaths


# Manual questions use a different vocabulary from legal definitions. A user
# asks to "file a case" while the manual has a section called "Filing a Case";
# asks to "sign" while the heading says "Signing a Document". The index is not
# stemmed (that would change legal retrieval), so the manual service expands a
# bounded set of workflow verbs with the forms the manual actually uses.
_MANUAL_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "file": ("filing", "filed", "files"),
    "filed": ("filing", "file", "files"),
    "filing": ("file", "filed", "files"),
    "sign": ("signing", "signed", "signature", "signatures"),
    "signed": ("signing", "sign", "signature"),
    "signing": ("sign", "signed", "signature"),
    "add": ("adding", "added", "adds"),
    "adding": ("add", "added", "adds"),
    "submit": ("submitting", "submitted", "submission"),
    "submitting": ("submit", "submitted", "submission"),
    "submitted": ("submit", "submitting", "submission"),
    "merge": ("merging", "merged", "merges"),
    "merging": ("merge", "merged", "merges"),
    "pay": ("payment", "payments", "paying"),
    "payment": ("payments", "pay", "paying"),
    "payments": ("payment", "pay", "paying"),
    "receipt": ("receipts",),
    "receipts": ("receipt",),
    "log": ("login", "logging", "logout"),
    "login": ("log", "logging"),
    "logout": ("log", "logging"),
    "register": ("registration", "registering", "registered"),
    "registration": ("register", "registering", "registered"),
    "reset": ("resetting", "resets"),
    "resetting": ("reset", "resets"),
    "browse": ("browsing", "browsed"),
    "browsing": ("browse", "browsed"),
    "sort": ("sorting", "sorted"),
    "filter": ("filtering", "filtered"),
    "search": ("searching", "searched"),
    "view": ("viewing", "viewed"),
    "export": ("exporting", "exported"),
    "change": ("changing", "changed"),
    "manage": ("managing", "managed"),
    "create": ("creating", "created"),
    "access": ("accessing", "accessed"),
    "enter": ("entering", "entered"),
    "click": ("clicking", "clicked"),
    "party": ("parties",),
    "parties": ("party",),
    "entity": ("entities",),
    "entities": ("entity",),
    "notification": ("notifications",),
    "notifications": ("notification",),
    "calendar": ("calendars",),
    "calendars": ("calendar",),
}


def _passage_order(passage_id: str) -> int:
    """Extract the block order from ``source#bN`` for reading-order sorting."""
    match = re.search(r"#b(\d+)$", passage_id)
    return int(match.group(1)) if match else 0


_STEP_START = re.compile(
    r"^\s*(?:step\s+)?(\d{1,2})\s*[.):\-]\s+", re.IGNORECASE
)


def expand_manual_query(query: str) -> str:
    """Add the manual's morphological variants to a workflow query."""
    terms = tokenize(query)
    if not terms:
        return query
    seen = set(terms)
    extra: list[str] = []
    for term in terms:
        for variant in _MANUAL_EXPANSIONS.get(term, ()):
            if variant not in seen:
                seen.add(variant)
                extra.append(variant)
    if not extra:
        return query
    return f"{query} {' '.join(extra[:24])}"


@dataclass(frozen=True)
class RetrievedPassage:
    """A passage of text plus everything needed to cite it honestly."""

    passage_id: str
    text: str
    source_id: str
    document_title: str
    issuing_authority: str
    document_type: str
    tier_label: str
    verification_status: str
    disclosure: str
    source_url: str = ""
    local_path: str = ""
    page_number: int | None = None
    section_heading: str = ""
    score: float = 0.0
    extraction_warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_synthetic(self) -> bool:
        return self.verification_status == "synthetic"


@dataclass(frozen=True)
class RetrievalResult:
    """Outcome of a retrieval query."""

    query: str
    passages: tuple[RetrievedPassage, ...] = ()
    sufficient: bool = False
    # Populated when evidence was found but is not allowed to ground an answer.
    excluded_statuses: tuple[str, ...] = ()
    # Populated when the registry is empty or the index was never built.
    diagnostics: tuple[str, ...] = ()
    # How much of the question the best passage covers: high, medium, low, none.
    confidence: str = "none"
    matched_terms: tuple[str, ...] = ()
    known_terms: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.passages


class RetrievalService:
    """Base retrieval contract."""

    def search(self, query: str, *, top_k: int | None = None) -> RetrievalResult:
        raise NotImplementedError


class LocalKeywordRetrievalService(RetrievalService):
    """Keyword retrieval over the locally built index."""

    def __init__(
        self,
        index: KeywordIndex,
        registry: dict[str, SourceMetadata],
        *,
        top_k: int = 4,
        min_score: float = 1.0,
        allow_synthetic: bool = False,
        source_ids: frozenset[str] | None = None,
        exclude_source_ids: frozenset[str] | None = None,
        scope_label: str = "",
        expand_query: Callable[[str], str] | None = None,
        expand_neighbors: bool = False,
    ) -> None:
        self.index = index
        self.registry = registry
        self.top_k = top_k
        self.min_score = min_score
        self.allow_synthetic = allow_synthetic
        # An include list turns the service into a scoped collection (for
        # example the IECMS manuals). An exclude list keeps one collection out
        # of the general legal corpus. ``None`` means no restriction.
        self.source_ids = source_ids
        self.exclude_source_ids = exclude_source_ids
        self.scope_label = scope_label
        self.expand_query = expand_query
        self.expand_neighbors = expand_neighbors

    # -- Helpers ----------------------------------------------------------

    def _passage_from_index(self, passage, *, score: float = 0.0) -> RetrievedPassage:
        metadata = self.registry.get(passage.source_id)
        if metadata is None:
            # A passage whose source is not registered cannot be trusted.
            metadata = SourceMetadata(
                source_id=passage.source_id,
                title=passage.source_id,
                issuing_authority="Unknown",
                verification_status="unverified",
                notes="Passage present in the index but missing from the registry.",
            )
        return RetrievedPassage(
            passage_id=passage.passage_id,
            text=passage.text,
            source_id=passage.source_id,
            document_title=metadata.title,
            issuing_authority=metadata.issuing_authority,
            document_type=metadata.document_type,
            tier_label=metadata.tier_label,
            verification_status=metadata.verification_status,
            disclosure=metadata.disclosure(allow_synthetic=self.allow_synthetic),
            source_url=metadata.source_url,
            local_path=metadata.local_path,
            page_number=passage.page_number,
            section_heading=passage.heading,
            score=score,
            extraction_warnings=tuple(metadata.extraction_warnings),
        )

    def _passage_from_hit(self, hit) -> RetrievedPassage:
        return self._passage_from_index(hit.passage, score=hit.score)

    def _with_neighbours(
        self, ranked: list[RetrievedPassage], limit: int, query: str
    ) -> list[RetrievedPassage]:
        """Add adjacent passages so a procedure is not cut in half.

        Keyword ranking may return "Step 4" first without "Step 1". For a
        manual, the surrounding passages on the same page or under the same
        heading are part of the answer. They are appended after the ranked
        results, then the whole single-source list is put back into reading
        order.
        """
        if not ranked:
            return ranked
        max_total = min(max(limit + 10, 14), 20)

        def same_group(candidate: RetrievedPassage, passage) -> bool:
            if passage.source_id != candidate.source_id:
                return False
            heading = (candidate.section_heading or "").strip()
            usable_heading = bool(heading) and not heading.lower().startswith(
                ("figure ", "table ")
            )
            if usable_heading:
                # A page can contain the end of one procedure and the start of
                # another; the section heading is the safer boundary.
                return passage.heading == candidate.section_heading
            if candidate.page_number is not None:
                return passage.page_number == candidate.page_number
            return False

        def group_for(candidate: RetrievedPassage):
            return sorted(
                (
                    passage
                    for passage in self.index.passages.values()
                    if same_group(candidate, passage)
                    and self._is_allowed(passage.source_id)
                ),
                key=lambda passage: _passage_order(passage.passage_id),
            )

        query_terms = set(tokenize(query))
        query_lower = query.lower()

        def seed_rank(candidate: RetrievedPassage):
            numbers = []
            for passage in group_for(candidate):
                match = _STEP_START.match(passage.text)
                if match:
                    numbers.append(int(match.group(1)))
            heading = (candidate.section_heading or "").lower()
            score = candidate.score
            # "Adding Legal Entity Parties" is a variant section. Do not let it
            # beat the general "Adding Individual Parties" section unless the
            # user explicitly asked about legal entities.
            if "legal entity" in heading and "legal entity" not in query_lower:
                score -= 8.0
            overlap = len(
                query_terms & set(tokenize(candidate.section_heading or ""))
            )
            # Prefer a section that starts at Step 1, then the closest match.
            return (1 if 1 in numbers else 0, score + overlap * 2.0, len(numbers))

        # Choose the section to expand from candidates close to the top score.
        # A low-scoring "Merging Documents" section must not displace the
        # actual "Filing a Case" match.
        top_score = ranked[0].score if ranked else 0.0
        candidates = [
            passage
            for passage in ranked[: min(len(ranked), 12)]
            if passage.score >= top_score * 0.5
        ] or ranked[: min(len(ranked), 4)]
        best = max(candidates, key=seed_rank)
        ranked_by_id = {passage.passage_id: passage for passage in ranked}
        group = [
            ranked_by_id.get(passage.passage_id)
            or self._passage_from_index(passage)
            for passage in group_for(best)
        ]
        group_ids = {passage.passage_id for passage in group}

        # Keep the ranked hits and add the neighbours of the next-best seeds.
        combined = [*ranked]
        selected_ids = {passage.passage_id for passage in combined}
        for seed in ranked[:3]:
            if seed.passage_id == best.passage_id:
                continue
            for passage in group_for(seed):
                if len(combined) >= max_total:
                    break
                if passage.passage_id in selected_ids:
                    continue
                selected_ids.add(passage.passage_id)
                combined.append(self._passage_from_index(passage))

        # Put the chosen section's own steps immediately after its opening,
        # before unrelated ranked passages.
        with_steps = self._append_procedure_steps(group, best, max_total)
        with_steps_ids = {passage.passage_id for passage in with_steps}
        remaining = [
            passage
            for passage in combined
            if passage.passage_id not in with_steps_ids
        ]
        return [*with_steps, *remaining][:max_total]

    def _append_procedure_steps(
        self,
        selected: list[RetrievedPassage],
        best: RetrievedPassage,
        max_total: int,
    ) -> list[RetrievedPassage]:
        """Append later numbered steps from the same procedure.

        A procedure can cross several PDF pages, so the page containing the
        matching passage may hold only the first two steps. Numbered steps that
        follow the best match in document order are added until the numbering
        restarts (which indicates a different procedure) or the result limit is
        reached.
        """
        if len(selected) >= max_total:
            return selected

        expected = 0
        for passage in selected:
            match = _STEP_START.match(passage.text)
            if match:
                expected = max(expected, int(match.group(1)))
        if expected == 0:
            return selected

        best_order = _passage_order(best.passage_id)
        candidates = sorted(
            (
                passage
                for passage in self.index.passages.values()
                if passage.source_id == best.source_id
                and _passage_order(passage.passage_id) > best_order
                and _STEP_START.match(passage.text)
            ),
            key=lambda passage: _passage_order(passage.passage_id),
        )

        selected_ids = {passage.passage_id for passage in selected}
        appended: list[RetrievedPassage] = []
        for passage in candidates:
            if len(selected) + len(appended) >= max_total:
                break
            match = _STEP_START.match(passage.text)
            if not match:
                continue
            number = int(match.group(1))
            if number <= expected:
                if passage.passage_id in selected_ids:
                    # Already part of the section we expanded.
                    continue
                # The numbering restarted: a different procedure begins.
                break
            if number > expected + 3:
                break
            if passage.passage_id in selected_ids:
                continue
            if not self._is_allowed(passage.source_id):
                continue
            selected_ids.add(passage.passage_id)
            appended.append(self._passage_from_index(passage))
            expected = number
        return [*selected, *appended]

    def _in_scope(self, source_id: str) -> bool:
        """Whether the source belongs to this service's collection."""
        if self.source_ids is not None and source_id not in self.source_ids:
            return False
        if self.exclude_source_ids and source_id in self.exclude_source_ids:
            return False
        return True

    def _is_allowed(self, source_id: str) -> bool:
        if not self._in_scope(source_id):
            return False
        metadata = self.registry.get(source_id)
        if metadata is None:
            return False
        return metadata.is_authoritative(allow_synthetic=self.allow_synthetic)

    # -- Contract ---------------------------------------------------------

    def search(self, query: str, *, top_k: int | None = None) -> RetrievalResult:
        diagnostics: list[str] = []

        if self.index.size == 0:
            diagnostics.append(
                "The knowledge base is empty. No approved legal sources have "
                "been ingested yet."
            )
            return RetrievalResult(
                query=query, sufficient=False, diagnostics=tuple(diagnostics)
            )

        if not self.registry:
            diagnostics.append("The source registry is empty.")

        limit = top_k or self.top_k
        # Fetch more than needed so filtered-out sources cannot starve the
        # result. A scoped collection needs a deeper candidate pool because the
        # index as a whole may be much larger than the manual collection.
        scoped = self.source_ids is not None or bool(self.exclude_source_ids)
        fetch_k = max(limit * (12 if scoped else 4), 60 if scoped else limit)
        search_query = self.expand_query(query) if self.expand_query else query
        hits = self.index.search(search_query, top_k=fetch_k)

        allowed: list[RetrievedPassage] = []
        excluded: set[str] = set()
        for hit in hits:
            if len(allowed) >= limit:
                break
            passage = self._passage_from_hit(hit)
            if not self._in_scope(passage.source_id):
                # A different collection matched; it is irrelevant here, not
                # an unapproved source.
                continue
            if self._is_allowed(passage.source_id):
                allowed.append(passage)
            else:
                excluded.add(passage.verification_status)

        if allowed and self.expand_neighbors:
            allowed = self._with_neighbours(allowed, limit, query)

        if not allowed:
            scope = self.scope_label or "the selected collection"
            if excluded:
                diagnostics.append(
                    "Matching text was found only in sources that are not "
                    "approved for authoritative answers (status: "
                    + ", ".join(sorted(excluded))
                    + ")."
                )
            elif self.source_ids is not None:
                diagnostics.append(
                    f"No passage in {scope} matched the question."
                )
            else:
                diagnostics.append(
                    "No passage in the knowledge base matched the question."
                )
            return RetrievalResult(
                query=query,
                passages=(),
                sufficient=False,
                excluded_statuses=tuple(sorted(excluded)),
                diagnostics=tuple(diagnostics),
            )

        best_passage = max(allowed, key=lambda passage: passage.score)
        sufficient = best_passage.score >= self.min_score
        if not sufficient:
            diagnostics.append(
                "The best match scored below the relevance threshold, so the "
                "available material does not establish a reliable answer."
            )

        # How much of the question the best passage actually covers. Keyword
        # search can match on an incidental word, so a partial match is
        # disclosed to the user rather than presented as a good answer.
        content_terms = set(query_terms(query))
        known_terms = {term for term in content_terms if self.index.postings.get(term)}
        passage_text = (
            f"{best_passage.document_title} {best_passage.section_heading} "
            f"{best_passage.text}"
        ).lower()
        matched_terms = {
            term
            for term in content_terms
            if term_in_text(term, passage_text, prefix_length=4)
        }
        if not content_terms:
            coverage = 0.0
            required = 1.0
        else:
            coverage = len(matched_terms) / len(content_terms)
            # Two-word questions need both concepts; longer questions may miss
            # one incidental word (for example "difference" or "case") but not
            # more. A passage covering half of a two-word subject is not an
            # answer to it.
            required = 1.0 if len(content_terms) <= 2 else 2.0 / 3.0
        if content_terms and coverage + 1e-9 < required:
            sufficient = False
            diagnostics.append(
                "The best passage covers only part of the question, so a "
                "partial match was not used as an answer."
            )
        if coverage >= 1.0:
            confidence = "high"
        elif coverage + 1e-9 >= required:
            confidence = "medium"
        else:
            confidence = "low"

        return RetrievalResult(
            query=query,
            passages=tuple(allowed),
            sufficient=sufficient,
            excluded_statuses=tuple(sorted(excluded)),
            diagnostics=tuple(diagnostics),
            confidence=confidence if sufficient else "none",
            matched_terms=tuple(sorted(matched_terms)),
            known_terms=tuple(sorted(known_terms)),
        )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

_SERVICE: LocalKeywordRetrievalService | None = None
_MANUAL_SERVICE: LocalKeywordRetrievalService | None = None


def get_corpus_paths() -> CorpusPaths:
    return CorpusPaths.default(settings.LEGAL_SOURCES_DIR)


def _manual_source_ids() -> frozenset[str]:
    return frozenset(getattr(settings, "IECMS_MANUAL_SOURCE_IDS", []) or [])


def build_retrieval_service() -> LocalKeywordRetrievalService:
    """Build a retrieval service for the general legal corpus.

    IECMS manuals are excluded: they are procedural guidance, not legal
    authorities for definitions. Manual questions use
    :func:`build_manual_retrieval_service` instead.
    """
    paths = get_corpus_paths()
    return LocalKeywordRetrievalService(
        KeywordIndex.load(paths.index_file),
        load_registry(paths.registry),
        top_k=settings.RETRIEVAL_TOP_K,
        min_score=settings.RETRIEVAL_MIN_SCORE,
        allow_synthetic=settings.ALLOW_SYNTHETIC_SOURCES,
        exclude_source_ids=_manual_source_ids(),
    )


def build_manual_retrieval_service() -> LocalKeywordRetrievalService:
    """Build a retrieval service scoped to the approved IECMS manuals."""
    paths = get_corpus_paths()
    return LocalKeywordRetrievalService(
        KeywordIndex.load(paths.index_file),
        load_registry(paths.registry),
        top_k=getattr(settings, "IECMS_MANUAL_TOP_K", settings.RETRIEVAL_TOP_K),
        min_score=getattr(
            settings, "IECMS_MANUAL_MIN_SCORE", settings.RETRIEVAL_MIN_SCORE
        ),
        allow_synthetic=settings.ALLOW_SYNTHETIC_SOURCES,
        source_ids=_manual_source_ids(),
        scope_label="the IECMS manual",
        expand_query=expand_manual_query,
        expand_neighbors=True,
    )


def get_retrieval_service() -> LocalKeywordRetrievalService:
    """Return the process-wide legal retrieval service, building it on first use."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = build_retrieval_service()
    return _SERVICE


def get_manual_retrieval_service() -> LocalKeywordRetrievalService:
    """Return the process-wide IECMS manual retrieval service."""
    global _MANUAL_SERVICE
    if _MANUAL_SERVICE is None:
        _MANUAL_SERVICE = build_manual_retrieval_service()
    return _MANUAL_SERVICE


def reset_retrieval_service() -> None:
    """Drop cached services. Used by tests and after re-indexing."""
    global _SERVICE, _MANUAL_SERVICE
    _SERVICE = None
    _MANUAL_SERVICE = None
