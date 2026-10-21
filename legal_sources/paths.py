"""Canonical filesystem locations for the knowledge base.

Kept in one place so that ingestion, indexing and the retrieval service cannot
disagree about where things live. All paths stay inside the repository and are
git-ignored except for the registry and the synthetic fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CorpusPaths:
    """Resolved paths for the legal-source corpus."""

    root: Path
    registry: Path
    documents_dir: Path
    extracted_dir: Path
    index_file: Path
    synthetic_dir: Path
    manifest: Path

    @classmethod
    def default(cls, base_dir: str | Path | None = None) -> "CorpusPaths":
        root = Path(base_dir) if base_dir else Path(__file__).resolve().parent
        return cls(
            root=root,
            registry=root / "registry.json",
            documents_dir=root / "documents",
            extracted_dir=root / "extracted",
            index_file=root / "index" / "index.json",
            synthetic_dir=root / "synthetic",
            manifest=root / "manifest.json",
        )

    def ensure_directories(self) -> None:
        for directory in (
            self.documents_dir,
            self.extracted_dir,
            self.index_file.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def extracted_path(self, source_id: str) -> Path:
        return self.extracted_dir / f"{source_id}.json"
