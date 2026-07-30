"""Score toutes les candidatures de toutes les offres ouvertes.

    python manage.py score_all
    python manage.py score_all --quiet

Sert au deploiement : un visiteur qui arrive sur un classement vide repart sans
avoir rien vu. Le calcul est deterministe et coute quelques millisecondes par
candidature, il n'appelle aucun modele — l'analyse redigee, elle, demande un
serveur d'inference et n'est donc jamais demandee ici.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from apps.jobs.models import JobOffer
from apps.matching.services import score_offer


class Command(BaseCommand):
    help = "Calcule le score de toutes les candidatures, sans appel modele."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet", action="store_true", help="N'affiche que le total."
        )
        parser.add_argument(
            "--all-statuses",
            action="store_true",
            help="Inclut les offres qui ne sont pas ouvertes.",
        )

    def handle(self, *args, **options):
        offres = JobOffer.objects.all()
        if not options["all_statuses"]:
            offres = offres.filter(status=JobOffer.Status.OPEN)

        debut = time.perf_counter()
        total = 0
        for offre in offres:
            scores = score_offer(offre, with_explanation=False)
            total += len(scores)
            if not options["quiet"]:
                self.stdout.write(f"  {offre.title:<44}{len(scores):>4} candidature(s)")

        duree = (time.perf_counter() - debut) * 1000
        self.stdout.write(
            self.style.SUCCESS(
                f"{total} candidature(s) scoree(s) en {duree:.0f} ms, "
                f"sans aucun appel modele."
            )
        )
