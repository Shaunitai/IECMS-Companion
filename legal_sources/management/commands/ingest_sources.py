"""Ingest sources listed in a manifest.

Each entry is either an authorised local file or an approved public URL. Fetch
failures are recorded as ``inaccessible`` with the real reason; contents are
never invented.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from chatbot.services.retrieval_service import reset_retrieval_service
from legal_sources.ingestion import SourceIngestionError, SourceIngestor
from legal_sources.manifests import load_manifest
from legal_sources.paths import CorpusPaths


class Command(BaseCommand):
    help = "Ingest local files or approved URLs listed in a manifest."

    def add_arguments(self, parser):
        parser.add_argument(
            "--manifest",
            default=None,
            help="Path to the manifest JSON (default: legal_sources/manifest.json).",
        )
        parser.add_argument(
            "--no-index",
            action="store_true",
            help="Skip rebuilding the search index afterwards.",
        )
        parser.add_argument(
            "--source",
            action="append",
            default=None,
            dest="sources",
            metavar="SOURCE_ID",
            help=(
                "Only ingest the manifest entries with this source id. "
                "Repeat the option to select several sources (useful for "
                "re-running slow OCR on scanned documents)."
            ),
        )

    def handle(self, *args, **options):
        paths = CorpusPaths.default(settings.LEGAL_SOURCES_DIR)
        manifest_path = options["manifest"] or paths.manifest

        if not manifest_path or not str(manifest_path):
            raise CommandError("No manifest supplied and no default manifest found.")

        manifest = load_manifest(manifest_path)
        if manifest.is_empty:
            self.stdout.write(self.style.WARNING("The manifest lists no sources."))
            return

        if manifest.synthetic:
            self.stdout.write(
                self.style.WARNING(
                    "This manifest is marked SYNTHETIC. Its entries are software "
                    "test fixtures, not official legal authorities."
                )
            )

        ingestor = SourceIngestor(
            paths,
            user_agent=settings.SOURCE_USER_AGENT,
            timeout=settings.SOURCE_FETCH_TIMEOUT,
            min_interval=settings.SOURCE_FETCH_MIN_INTERVAL,
            allowed_hosts=settings.SOURCE_ALLOWED_HOSTS,
            respect_robots=settings.SOURCE_RESPECT_ROBOTS,
        )

        entries = list(manifest.entries)
        selected = set(options.get("sources") or [])
        if selected:
            entries = [entry for entry in entries if entry.source_id in selected]
            missing = selected - {entry.source_id for entry in entries}
            if missing:
                self.stdout.write(
                    self.style.WARNING(
                        "Not in this manifest: " + ", ".join(sorted(missing))
                    )
                )
            if not entries:
                raise CommandError(
                    "None of the selected source ids appear in the manifest."
                )

        failures = 0
        for entry in entries:
            try:
                if entry.path is not None:
                    metadata = ingestor.ingest_local_file(entry.path, entry.metadata)
                    self.stdout.write(
                        f"  ingested  {entry.source_id}  ({metadata.verification_status})"
                    )
                elif entry.url:
                    metadata = ingestor.ingest_url(entry.url, entry.metadata)
                    status = metadata.verification_status
                    if status == "inaccessible":
                        failures += 1
                        self.stdout.write(
                            self.style.WARNING(
                                f"  FAILED    {entry.source_id}  "
                                f"{' '.join(metadata.extraction_warnings)}"
                            )
                        )
                    else:
                        self.stdout.write(f"  ingested  {entry.source_id}  ({status})")
                else:
                    failures += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"  SKIPPED   {entry.source_id}  (no path or url)"
                        )
                    )
            except (SourceIngestionError, FileNotFoundError, ValueError) as exc:
                failures += 1
                self.stdout.write(self.style.ERROR(f"  ERROR     {entry.source_id}  {exc}"))

        if not options["no_index"]:
            index = ingestor.rebuild_index()
            reset_retrieval_service()
            self.stdout.write(f"Indexed {index.size} passage(s).")

        if failures:
            self.stdout.write(
                self.style.WARNING(f"{failures} source(s) were not ingested successfully.")
            )
