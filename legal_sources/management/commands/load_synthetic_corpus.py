"""Load the bundled SYNTHETIC fixtures and rebuild the index.

Development and testing only. These fixtures are not official legal authorities
and answers built from them always disclose that fact.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from chatbot.services.retrieval_service import reset_retrieval_service
from legal_sources.ingestion import SourceIngestor
from legal_sources.manifests import load_manifest
from legal_sources.paths import CorpusPaths


class Command(BaseCommand):
    help = (
        "Load the bundled SYNTHETIC test fixtures into the corpus and rebuild "
        "the index. These fixtures are not legal authorities."
    )

    def handle(self, *args, **options):
        if not settings.ALLOW_SYNTHETIC_SOURCES:
            raise CommandError(
                "Synthetic sources are disabled (ALLOW_SYNTHETIC_SOURCES=False). "
                "Enable them only in development or test environments."
            )

        paths = CorpusPaths.default(settings.LEGAL_SOURCES_DIR)
        manifest = load_manifest(paths.manifest)

        if not manifest.synthetic:
            raise CommandError(
                "The default manifest is not marked as synthetic; refusing to "
                "load it through this command. Use 'ingest_sources' instead."
            )

        ingestor = SourceIngestor(paths, allowed_hosts=settings.SOURCE_ALLOWED_HOSTS)

        self.stdout.write(
            self.style.WARNING(
                "Loading SYNTHETIC test fixtures. They are not official legal "
                "authorities and must not be presented as law."
            )
        )
        for entry in manifest.entries:
            metadata = ingestor.ingest_local_file(entry.path, entry.metadata)
            self.stdout.write(
                f"  loaded    {entry.source_id}  ({metadata.verification_status})"
            )

        index = ingestor.rebuild_index()
        reset_retrieval_service()
        self.stdout.write(
            self.style.SUCCESS(
                f"Indexed {index.size} passage(s) from {len(index.source_ids)} "
                "synthetic source(s)."
            )
        )
