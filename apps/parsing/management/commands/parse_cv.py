"""Extraction d'un CV en ligne de commande.

    python manage.py parse_cv chemin/vers/cv.pdf
    python manage.py parse_cv chemin/vers/cv.pdf --no-llm   # diagnostic seul

`--no-llm` s'arrete apres l'extraction du texte et le diagnostic de qualite :
utile pour verifier la detection multi-colonnes ou scan sans serveur
d'inference joignable.
"""

from __future__ import annotations

import time
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from apps.parsing import extractors, quality
from apps.parsing.services import ingest


class Command(BaseCommand):
    help = "Extrait un CV depuis un fichier local et affiche le diagnostic."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Chemin du fichier PDF ou DOCX.")
        parser.add_argument(
            "--no-llm",
            action="store_true",
            help="Diagnostic d'extraction uniquement, sans appel au modele.",
        )
        parser.add_argument("--offer", help="Slug d'une offre a laquelle rattacher le CV.")

    def handle(self, *args, **options):
        path = Path(options["path"]).expanduser().resolve()
        if not path.is_file():
            raise CommandError(f"Fichier introuvable : {path}")

        data = path.read_bytes()
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n== {path.name} =="))
        self.stdout.write(f"Taille          {len(data) / 1024:.1f} Ko")

        started = time.perf_counter()
        extracted = extractors.extract(data, path.name)
        report = quality.assess(extracted)
        elapsed = (time.perf_counter() - started) * 1000

        self.stdout.write(f"Pages           {extracted.page_count}")
        self.stdout.write(f"Caracteres      {report.char_count} ({report.chars_per_page}/page)")
        self.stdout.write(f"Extraction      {elapsed:.0f} ms")
        self.stdout.write(
            "Scan presume    "
            + (self.style.WARNING("oui") if report.looks_scanned else "non")
        )
        self.stdout.write(
            "Multi-colonnes  "
            + (
                self.style.WARNING(f"pages {report.multi_column_pages}")
                if report.is_multi_column
                else "non"
            )
        )
        voie = "vision (Qwen3-VL)" if report.needs_vision else "texte (Qwen3.6)"
        self.stdout.write(f"Voie retenue    {voie}")

        if options["no_llm"]:
            self.stdout.write("\n--- apercu du texte extrait ---")
            self.stdout.write(extracted.full_text[:800] or "(vide)")
            self.stdout.write(self.style.SUCCESS("\nDiagnostic termine (--no-llm).\n"))
            return

        offer = None
        if options["offer"]:
            from apps.jobs.models import JobOffer

            offer = JobOffer.objects.filter(slug=options["offer"]).first()
            if offer is None:
                raise CommandError(f"Offre introuvable : {options['offer']}")

        upload = SimpleUploadedFile(path.name, data)
        document, created = ingest(upload, offer=offer)
        document.refresh_from_db()

        if document.status != document.Status.DONE:
            raise CommandError(f"Extraction en echec : {document.error}")

        candidate = document.candidate
        spans = document.spans.all()
        verified = sum(1 for span in spans if span.verified)

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Resultat =="))
        self.stdout.write(f"Methode         {document.get_method_display()}")
        self.stdout.write(f"Duree totale    {document.extraction_seconds:.1f} s")
        self.stdout.write(f"Candidat        {candidate.full_name} <{candidate.email}>")
        self.stdout.write(f"Titre           {candidate.headline or '—'}")
        self.stdout.write(f"Experience      {candidate.total_experience_years:.1f} ans")
        self.stdout.write(f"Competences     {candidate.skills.count()}")
        self.stdout.write(f"Experiences     {candidate.experiences.count()}")
        self.stdout.write(f"Formations      {candidate.education.count()}")
        self.stdout.write(f"Langues         {candidate.languages.count()}")

        if spans:
            rate = verified / len(spans) * 100
            style = self.style.SUCCESS if rate >= 80 else self.style.WARNING
            self.stdout.write(
                "Preuves ancrees " + style(f"{verified}/{len(spans)} ({rate:.0f} %)")
            )
            unverified = [span for span in spans if not span.verified]
            if unverified:
                self.stdout.write(
                    self.style.WARNING(
                        "\nCitations introuvables dans le document "
                        "(donnees marquees non etayees) :"
                    )
                )
                for span in unverified[:5]:
                    self.stdout.write(f"  · {span.text[:100]}")

        self.stdout.write(self.style.SUCCESS(f"\nCandidat : {candidate.get_absolute_url()}\n"))
