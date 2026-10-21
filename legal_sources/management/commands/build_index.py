"""Rebuild the local keyword index from the source registry."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from chatbot.services.retrieval_service import reset_retrieval_service
from legal_sources.ingestion import SourceIngestor
from legal_sources.paths import CorpusPaths


class Command(BaseCommand):
    help = "Rebuild the local keyword index from the registered sources."

    def handle(self, *args, **options):
        paths = CorpusPaths.default(settings.LEGAL_SOURCES_DIR)
        ingestor = SourceIngestor(
            paths,
            user_agent=settings.SOURCE_USER_AGENT,
            timeout=settings.SOURCE_FETCH_TIMEOUT,
            min_interval=settings.SOURCE_FETCH_MIN_INTERVAL,
            allowed_hosts=settings.SOURCE_ALLOWED_HOSTS,
            respect_robots=settings.SOURCE_RESPECT_ROBOTS,
        )
        index = ingestor.rebuild_index()
        reset_retrieval_service()
        self.stdout.write(
            self.style.SUCCESS(
                f"Indexed {index.size} passage(s) from {len(index.source_ids)} source(s)."
            )
        )
