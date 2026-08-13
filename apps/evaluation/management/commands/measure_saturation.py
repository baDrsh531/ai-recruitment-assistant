"""Mesure la saturation du score sur le jeu annote.

    python manage.py measure_saturation

Le moteur ramene chaque critere dans [0, 1] : un profil qui satisfait toutes les
exigences atteint 1.0, et plusieurs profils differents peuvent y arriver
ensemble. Au plafond, l'ordre ne vient plus du score.

**La commande ne corrige rien et ne propose rien.** Elle chiffre l'ampleur du
probleme — combien de profils, sur quels cas, a partir de quel effectif — pour
qu'un arbitrage sur la ponderation se prenne sur des mesures. Un recruteur peut
tres bien vouloir qu'un profil parfait soit parfait.

Aucun appel au modele.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.evaluation import saturation


class Command(BaseCommand):
    help = "Compte les profils au plafond du score. Sans token, sans correction."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default="ranking_v1")

    def handle(self, *args, **options):
        mesure = saturation.mesurer(options["dataset"])

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Saturation du score =="))
        self.stdout.write(f"  plafond retenu    {saturation.PLAFOND}")
        self.stdout.write(f"  cas               {mesure.cas_total}")
        self.stdout.write(f"  candidats         {mesure.candidats_total}")
        self.stdout.write(
            f"  au plafond        {mesure.au_plafond} "
            f"({mesure.part_au_plafond * 100:.1f} %)"
        )

        self.stdout.write(f"\n  {mesure.lecture}")

        if not mesure.satures:
            return

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Cas concernes =="))
        self.stdout.write(
            f"  {'cas':<34}{'vivier':>7}{'plafond':>9}{'pertinences':>14}"
        )
        for item in sorted(mesure.satures, key=lambda x: -x.au_plafond):
            style = self.style.ERROR if item.pertinences_confondues else str
            pertinences = ",".join(str(valeur) for valeur in item.pertinences)
            self.stdout.write(
                style(
                    f"  {item.cas:<34}{item.candidats:>7}{item.au_plafond:>9}"
                    f"{pertinences:>14}"
                )
            )

        if mesure.confusions:
            self.stdout.write(
                self.style.MIGRATE_HEADING("\n== Ce que la saturation confond ==")
            )
            for item in mesure.confusions:
                self.stdout.write(self.style.ERROR(f"  {item.cas}"))
                for identifiant, pertinence in zip(
                    item.identifiants, item.pertinences, strict=True
                ):
                    self.stdout.write(
                        f"      {identifiant:<24} pertinence {pertinence}"
                    )
            self.stdout.write(
                "\n  Ces profils sont a egalite pour le moteur et separes par "
                "l'annotation.\n  L'ordre entre eux ne vient donc pas du score."
            )
            if mesure.effectif_declencheur:
                self.stdout.write(
                    f"\n  Apparait des {mesure.effectif_declencheur} candidats "
                    f"dans un meme vivier."
                )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "\n  Aucune confusion : partout ou plusieurs profils sont au "
                    "plafond,\n  l'annotation les tient pour equivalents."
                )
            )

        self.stdout.write(
            "\nCette commande ne corrige rien. Etaler le haut de l'echelle ou "
            "revoir la\nponderation est un arbitrage produit, pas un defaut a "
            "reparer seul."
        )
