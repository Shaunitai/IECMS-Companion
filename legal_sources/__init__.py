"""Source ingestion, extraction, metadata and indexing package.

This package will hold the legal-source pipeline (Phase 2):

- ``ingestion``  : fetch/read authorised local files and approved public URLs.
- ``extraction`` : extract text plus page/section references from documents.
- ``metadata``   : source metadata model and verification status.
- ``indexing``   : build and query the local searchable index.

Nothing in this package is implemented in Phase 1.
"""
