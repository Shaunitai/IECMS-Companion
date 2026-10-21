"""Local keyword index over the approved knowledge base.

This is a deliberately simple, dependency-free inverted index with a
BM25-style score. The retrieval interface in
``chatbot.services.retrieval_service`` wraps it, so a hybrid or vector
implementation can replace it later without touching the chat application.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from .extraction import ExtractedDocument
from .metadata import SourceMetadata

TOKEN_RE = re.compile(r"[a-z0-9']+")

# A short stop-word list. Kept small and explicit rather than pulling in a
# linguistics dependency; legal terms such as "order" or "notice" must survive.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by for from had has have he her his i if in
    into is it its me my no not of on or our she so than that the their them
    then there these they this to too was we were what when where which who
    whom why will with you your
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with stop words removed."""
    return [
        token
        for token in TOKEN_RE.findall(text.lower())
        if token not in STOPWORDS and len(token) > 1
    ]


# Question wording that carries no subject matter. It must not count towards
# how much of a question a passage covers, otherwise "how do I register?"
# appears to be a good match for a passage that merely contains "do".
QUERY_FILLERS = frozenset(
    """
    how do does did can could should would will what where when which who
    whom why please tell explain describe give show about general terms
    """.split()
)


def query_terms(text: str) -> list[str]:
    """Meaningful query terms, with question filler removed."""
    return [term for term in tokenize(text) if term not in QUERY_FILLERS]


def term_in_text(term: str, text: str, *, prefix_length: int = 5) -> bool:
    """Whether ``term`` (or a common inflection of it) appears in ``text``.

    The index is not stemmed, so a short prefix comparison is used for
    inflections: ``orders`` matches ``order`` and ``register`` matches
    ``registration``. An ``e``-drop form is also recognised, so ``file``
    matches ``filing`` and ``write`` matches ``written``. Short words must
    appear as written.
    """
    if term in text:
        return True
    if len(term) > prefix_length and term[:prefix_length] in text:
        return True
    if term.endswith("e") and len(term) > 4 and term[:-1] in text:
        return True
    return False


# --- BM25 scoring parameters ------------------------------------------------
# k1 controls term-frequency saturation; b controls length normalisation.
#
# b is deliberately moderate. With b = 0 (no normalisation) a long statute that
# merely repeats a word outranks the short sentence that actually defines it.
# The textbook b = 0.75 over-corrects the other way and lets one-line extraction
# fragments (a bare form heading, a page number) win.
BM25_K1 = 1.2
BM25_B = 0.5

# A query term appearing in a passage's heading is a strong relevance signal,
# particularly for "what is X?" questions.
HEADING_BOOST = 2.0

# Passages shorter than this are usually stray headings, page numbers or form
# placeholders. They are damped rather than banned, so that a genuinely short
# definition can still rank.
MIN_SUBSTANTIVE_TOKENS = 6


@dataclass(frozen=True)
class IndexedPassage:
    """A searchable passage and the metadata needed to cite it."""

    passage_id: str
    source_id: str
    text: str
    page_number: int | None = None
    heading: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "IndexedPassage":
        return cls(**data)


@dataclass
class SearchHit:
    """A scored passage returned by a search."""

    passage: IndexedPassage
    metadata: SourceMetadata | None
    score: float


class KeywordIndex:
    """Inverted index with BM25-style scoring."""

    VERSION = 2

    def __init__(self) -> None:
        self.passages: dict[str, IndexedPassage] = {}
        self.postings: dict[str, dict[str, int]] = defaultdict(dict)
        self.document_frequency: Counter[str] = Counter()
        self.source_ids: set[str] = set()
        # Content-token count per passage, kept so BM25 length normalisation
        # does not have to re-tokenise every candidate on each query.
        self.lengths: dict[str, int] = {}

    # -- Construction -----------------------------------------------------

    @classmethod
    def build(cls, documents: list[tuple[SourceMetadata, ExtractedDocument]]) -> "KeywordIndex":
        """Build an index from (metadata, extracted document) pairs.

        Documents whose extraction produced no blocks are indexed as an empty
        source: they remain visible in the registry but contribute no passages.
        """
        index = cls()
        for metadata, document in documents:
            index.source_ids.add(metadata.source_id)
            for block in document.blocks:
                passage = IndexedPassage(
                    passage_id=block.block_id,
                    source_id=metadata.source_id,
                    text=block.text,
                    page_number=block.page_number,
                    heading=block.heading,
                )
                index.add(passage)
        return index

    def add(self, passage: IndexedPassage) -> None:
        tokens = tokenize(passage.text)
        self.passages[passage.passage_id] = passage
        self.lengths[passage.passage_id] = len(tokens)
        counts = Counter(tokens)
        for term, frequency in counts.items():
            self.postings[term][passage.passage_id] = frequency
            self.document_frequency[term] += 1

    # -- Search -----------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self.passages)

    @property
    def average_length(self) -> float:
        """Mean content-token count per passage, used for BM25 normalisation."""
        if not self.lengths:
            return 1.0
        return max(sum(self.lengths.values()) / len(self.lengths), 1.0)

    def _idf(self, term: str) -> float:
        """BM25 inverse document frequency for ``term``."""
        frequency = self.document_frequency.get(term, 0)
        if frequency == 0:
            return 0.0
        total = max(self.size, 1)
        return math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))

    def _term_weight(self, term: str, frequency: int, length: int, average: float) -> float:
        """BM25 saturation and length normalisation for one term in one passage."""
        normalisation = 1.0 - BM25_B + BM25_B * length / average
        return frequency * (BM25_K1 + 1.0) / (frequency + BM25_K1 * normalisation)

    def search(self, query: str, *, top_k: int = 4) -> list[SearchHit]:
        """Return the highest-scoring passages for ``query`` (empty if none)."""
        terms = tokenize(query)
        if not terms or not self.postings:
            return []

        average = self.average_length
        scores: dict[str, float] = {}
        heading_tokens: dict[str, set[str]] = {}

        for term in set(terms):
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self._idf(term)
            if idf <= 0.0:
                continue
            for passage_id, frequency in postings.items():
                passage = self.passages.get(passage_id)
                if passage is None:
                    continue
                length = self.lengths.get(passage_id)
                if length is None:
                    length = len(tokenize(passage.text))
                weight = self._term_weight(term, frequency, length, average)
                if HEADING_BOOST:
                    tokens = heading_tokens.get(passage_id)
                    if tokens is None:
                        tokens = set(tokenize(passage.heading))
                        heading_tokens[passage_id] = tokens
                    if term in tokens:
                        weight *= 1.0 + HEADING_BOOST
                # Damp stray headings, page numbers and form placeholders.
                weight *= min(1.0, length / MIN_SUBSTANTIVE_TOKENS)
                contribution = idf * weight
                if contribution <= 0.0:
                    continue
                scores[passage_id] = scores.get(passage_id, 0.0) + contribution

        if not scores:
            return []

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        hits: list[SearchHit] = []
        for passage_id, score in ranked[: max(top_k, 1)]:
            passage = self.passages.get(passage_id)
            if passage is not None:
                hits.append(SearchHit(passage=passage, metadata=None, score=round(score, 6)))
        return hits

    def passages_for_source(self, source_id: str) -> list[IndexedPassage]:
        return [p for p in self.passages.values() if p.source_id == source_id]

    # -- Persistence ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": self.VERSION,
            "passages": {pid: p.to_dict() for pid, p in self.passages.items()},
            "postings": {term: dict(postings) for term, postings in self.postings.items()},
            "lengths": dict(self.lengths),
            "source_ids": sorted(self.source_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KeywordIndex":
        index = cls()
        for pid, payload in data.get("passages", {}).items():
            index.passages[pid] = IndexedPassage.from_dict(payload)
        for term, postings in data.get("postings", {}).items():
            index.postings[term] = dict(postings)
            index.document_frequency[term] = len(postings)
        index.source_ids = set(data.get("source_ids", []))
        lengths = data.get("lengths")
        if isinstance(lengths, dict):
            index.lengths = {str(pid): int(count) for pid, count in lengths.items()}
        else:
            # Index written by an earlier version: measure the passages once.
            index.lengths = {
                pid: len(tokenize(passage.text))
                for pid, passage in index.passages.items()
            }
        return index

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> "KeywordIndex":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
