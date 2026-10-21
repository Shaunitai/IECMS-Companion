"""Mark a registered source as verified.

Verification is a deliberate human act: nothing in the pipeline sets this status
automatically, because a document is never treated as current merely because it
is still available online.
"""

from __future__ import annotations

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from chatbot.services.retrieval_service import reset_retrieval_service
from legal_sources.ingestion import SourceIngestionError, SourceIngestor
from legal_sources.metadata import VerificationStatus, load_registry, save_registry
from legal_sources.paths import CorpusPaths


class Command(BaseCommand):
    help = "Record a source's verification status and the date it was verified."

    def add_arguments(self, parser):
        parser.add_argument("source_id", help="The registered source identifier.")
        parser.add_argument(
            "--status",
            default=VerificationStatus.VERIFIED.value,
            choices=[status.value for status in VerificationStatus],
            help="The new verification status (default: verified).",
        )
        parser.add_argument(
            "--date",
            default=None,
            help="Verification date in YYYY-MM-DD form (default: today).",
        )
        parser.add_argument("--note", default="", help="Optional note to append.")

    def handle(self, *args, **options):
        paths = CorpusPaths.default(settings.LEGAL_SOURCES_DIR)
        registry = load_registry(paths.registry)
        source_id = options["source_id"]

        if source_id not in registry:
            raise CommandError(
                f"Unknown source '{source_id}'. Known sources: "
                + (", ".join(sorted(registry)) or "none")
            )

        metadata = registry[source_id]
        verified_on = options["date"] or date.today().isoformat()
        note = metadata.notes
        if options["note"]:
            note = f"{note} | {options['note']}".strip(" |")

        updated = metadata.__class__(
            **{
                **metadata.to_dict(),
                "verification_status": options["status"],
                "last_verified": verified_on,
                "notes": note,
            }
        )
        registry[source_id] = updated
        save_registry(paths.registry, registry)
        reset_retrieval_service()

        self.stdout.write(
            self.style.SUCCESS(
                f"{source_id} is now '{updated.verification_status}' "
                f"(verified {updated.last_verified})."
            )
        )
        if updated.verification_status != VerificationStatus.VERIFIED.value:
            self.stdout.write(
                self.style.WARNING(
                    "Only 'verified' sources may ground an authoritative answer."
                )
            )
