"""Purge les dossiers de candidature arrives a echeance.

    python manage.py purge_expired --dry-run
    python manage.py purge_expired

Sans `--dry-run`, la suppression est definitive et en cascade : CV, profil
extrait, preuves, scores, questions d'entretien. Seul le journal d'audit
survit, avec le compte et les identifiants supprimes — jamais de donnee
nominative.

Destinee a tourner quotidiennement (`purge_expired_task` cote Celery).
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.candidates import retention


class Command(BaseCommand):
    help = "Supprime les dossiers dont la duree de conservation est depassee."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", dest="dry_run",
            help="Montre ce qui serait supprime sans rien detruire.",
        )

    def handle(self, *args, **options):
        aujourdhui = dt.date.today()
        a_venir = retention.expiring_soon().count()

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Conservation des dossiers =="))
        self.stdout.write(f"Duree de conservation   {settings.DATA_RETENTION_DAYS} jours")
        self.stdout.write(f"Date du jour            {aujourdhui:%d/%m/%Y}")
        self.stdout.write(
            f"Echeance sous {retention.WARNING_WINDOW_DAYS} jours   {a_venir} dossier(s)"
        )

        rapport = retention.purge(dry_run=options["dry_run"])

        if rapport.nothing_to_do:
            self.stdout.write(
                self.style.SUCCESS("\nAucun dossier arrive a echeance.\n")
            )
            return

        self.stdout.write(f"\nDossiers echus          {rapport.due}")
        for nom in rapport.names:
            self.stdout.write(f"  · {nom}")
        if rapport.due > len(rapport.names):
            self.stdout.write(f"  … et {rapport.due - len(rapport.names)} autre(s)")

        if rapport.dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nSimulation : rien n'a ete supprime. Relancez sans "
                    "--dry-run pour purger.\n"
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{rapport.deleted} dossier(s) supprime(s). "
                "La purge est inscrite au journal d'audit.\n"
            )
        )
