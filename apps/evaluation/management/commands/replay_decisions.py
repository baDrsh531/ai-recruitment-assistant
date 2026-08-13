"""Rejoue les decisions passees avec le moteur d'aujourd'hui.

    python manage.py replay_decisions
    python manage.py replay_decisions --strict   # echoue si le score a bouge
                                                 # a version de moteur egale

`--strict` est fait pour l'integration continue. C'est le seul controle du
projet qui eprouve la reproductibilite sur des **decisions reelles** plutot que
sur un jeu annote : un moteur qui rendrait deux chiffres differents pour la
meme version et les memes donnees ferait echouer la construction.

Aucun appel au modele : tout est deterministe.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from apps.evaluation import replay


class Command(BaseCommand):
    help = "Recalcule les decisions tranchees et compare au score d'alors."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--afficher", type=int, default=15)
        parser.add_argument(
            "--strict", action="store_true",
            help="Sort en erreur si un score a bouge a version de moteur egale.",
        )

    def handle(self, *args, **options):
        rapport = replay.rejouer(limit=options["limit"])

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Rejeu =="))
        self.stdout.write(f"  moteur courant   {rapport.version_courante}")
        self.stdout.write(f"  decisions        {rapport.total}")
        self.stdout.write(
            f"  rejouables       {len(rapport.concluants)} "
            f"({len(rapport.non_concluants)} ecartees, donnees modifiees depuis)"
        )
        self.stdout.write(f"  identiques       {len(rapport.identiques)}")
        self.stdout.write(f"  ecarts           {len(rapport.divergents)}")
        self.stdout.write(f"  auraient bascule {len(rapport.bascules)}")
        if rapport.sans_score:
            self.stdout.write(
                f"  sans score       {rapport.sans_score} "
                f"(tranchees avant tout calcul, rien a comparer)"
            )
        if rapport.ecart_median is not None:
            self.stdout.write(f"  ecart median     {rapport.ecart_median} pts")

        self.stdout.write(f"\n  {rapport.lecture}")

        if rapport.par_transition:
            self.stdout.write(
                self.style.MIGRATE_HEADING("\n== Ce qui a fait bouger les scores ==")
            )
            for ligne in rapport.par_transition:
                marque = (
                    self.style.ERROR("  A VERSION EGALE")
                    if ligne["de"] == ligne["vers"]
                    else ""
                )
                self.stdout.write(
                    f"  {ligne['de']} -> {ligne['vers']} : {ligne['nombre']} "
                    f"dossier(s), {ligne['bascules']} bascule(s), "
                    f"ecart max {ligne['ecart_max']} pts{marque}"
                )

        a_montrer = rapport.bascules or rapport.divergents
        if a_montrer:
            self.stdout.write(self.style.MIGRATE_HEADING("\n== Dossiers =="))
            for item in a_montrer[: options["afficher"]]:
                style = self.style.ERROR if item.bascule else self.style.WARNING
                self.stdout.write(
                    style(
                        f"  {item.application.candidate.full_name:<26}"
                        f"{item.score_alors:.3f} -> {item.score_maintenant:.3f}  "
                        f"({item.points:+.1f} pts)  {item.gravite}"
                    )
                )

        if not rapport.reproductible:
            self.stdout.write(
                self.style.ERROR(
                    "\nDes scores ont bouge a version de moteur egale. C'est un "
                    "defaut de reproductibilite, pas une evolution."
                )
            )
            if options["strict"]:
                sys.exit(1)
            return

        self.stdout.write(
            self.style.SUCCESS(
                "\nAucun ecart a version de moteur egale : le score est "
                "reproductible sur les decisions reelles."
            )
        )
