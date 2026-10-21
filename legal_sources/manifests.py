"""Reading ingestion manifests.

A manifest is a JSON file listing sources to ingest. Each entry names either a
local file (``path``, relative to the manifest) or an approved public URL
(``url``), plus its metadata.

The bundled ``legal_sources/manifest.json`` contains **synthetic fixtures only**
and is marked as such. A manifest for real sources must list real, approved
documents with a real verification status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .metadata import SourceMetadata


@dataclass(frozen=True)
class ManifestEntry:
    """One source to ingest."""

    source_id: str
    metadata: SourceMetadata
    path: Path | None = None
    url: str = ""


@dataclass(frozen=True)
class Manifest:
    """A parsed manifest."""

    path: Path
    synthetic: bool
    note: str
    entries: tuple[ManifestEntry, ...]

    @property
    def is_empty(self) -> bool:
        return not self.entries


def load_manifest(path: str | Path) -> Manifest:
    """Parse a manifest file, resolving local paths relative to it."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    entries: list[ManifestEntry] = []

    for item in raw.get("sources", []):
        metadata = SourceMetadata.from_dict({**item.get("metadata", {}), "source_id": item["source_id"]})
        local = item.get("path")
        entries.append(
            ManifestEntry(
                source_id=item["source_id"],
                metadata=metadata,
                path=(base / local).resolve() if local else None,
                url=item.get("url", ""),
            )
        )

    return Manifest(
        path=path,
        synthetic=bool(raw.get("synthetic", False)),
        note=raw.get("note", ""),
        entries=tuple(entries),
    )
