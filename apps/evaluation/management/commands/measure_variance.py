"""Mesure ce que le modele fait varier, et ce qu'il ne touche jamais.

    python manage.py measure_variance --candidature <uuid>
    python manage.py measure_variance --tirages 5

Le projet affirme que le modele de langage n'attribue aucune note : il commente
un chiffre deja calcule. Cette commande le verifie en demandant N fois
l'analyse du meme score.

**Elle appelle le modele N fois et coute donc des tokens.** Le nombre de
tirages reste modeste par defaut, et la commande le rappelle avant de partir.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.candidates.models import Application
from apps.evaluation import variance


class Command(BaseCommand):
    help = "Redige N fois l'analyse du meme score et compare. Coute des tokens."

    def add_arguments(self, parser):
        parser.add_argument("--candidature", default=None, help="UUID d'une candidature.")
        parser.add_argument("--tirages", type=int, default=3)

    def handle(self, *args, **options):
        candidature = self._candidature(options["candidature"])
        tirages = max(2, options["tirages"])

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Variance du modele =="))
        self.stdout.write(f"  dossier   {candidature.candidate.full_name}")
        self.stdout.write(f"  offre     {candidature.offer.title}")
        self.stdout.write(
            self.style.WARNING(f"  {tirages} appels au modele — cela consomme des tokens.")
        )

        mesure = variance.mesurer(candidature, tirages=tirages)

        if mesure.indisponible:
            self.stdout.write(self.style.WARNING(f"\n  {mesure.indisponible}"))
            return

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Ce qui n'a pas bouge =="))
        self.stdout.write(
            f"  score     {mesure.score:.4f} sur les {mesure.nombre} tirages"
        )
        self.stdout.write(
            "            non recalcule : passe en entree au modele, qui ne "
            "peut que le mettre en mots"
        )

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Ce qui a bouge =="))
        self.stdout.write(f"  vocabulaire commun  {mesure.recouvrement_median}")
        self.stdout.write(f"  longueurs           {mesure.longueurs} mots")
        self.stdout.write(f"  amplitude           {mesure.ecart_de_longueur} mots")

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Chiffres cites =="))
        attendus = sorted(mesure.chiffres_attendus)
        self.stdout.write(f"  autorises par le score : {attendus}")
        for index, tirage in enumerate(mesure.tirages, start=1):
            self.stdout.write(f"  tirage {index} : {tirage.pourcentages_cites}")

        if mesure.fidele:
            self.stdout.write(
                self.style.SUCCESS(
                    "\n  Aucun chiffre invente : le modele reformule, il ne "
                    "recalcule pas."
                )
            )
        else:
            for index, valeur in mesure.chiffres_inventes:
                self.stdout.write(
                    self.style.ERROR(
                        f"  tirage {index} : « {valeur} % » ne correspond a aucun "
                        f"chiffre du score."
                    )
                )

        self.stdout.write(f"\n  {mesure.lecture}")

    def _candidature(self, identifiant: str | None) -> Application:
        candidatures = Application.objects.select_related(
            "candidate", "offer"
        ).filter(scores__isnull=False)
        if identifiant:
            candidature = candidatures.filter(pk=identifiant).first()
            if candidature is None:
                raise CommandError(f"Candidature introuvable ou sans score : {identifiant}")
            return candidature

        candidature = candidatures.first()
        if candidature is None:
            raise CommandError(
                "Aucune candidature scoree. Lancer `python manage.py seed_demo` "
                "puis un calcul de score."
            )
        return candidature
