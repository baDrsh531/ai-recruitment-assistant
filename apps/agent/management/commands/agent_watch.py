"""Veille de l'agent : recontrole le biais, date le releve, alerte.

    python manage.py agent_watch
    python manage.py agent_watch --strict    # code de sortie 1 s'il y a une alerte

Cette tache ne coute aucun token — le ratio d'impact se calcule par le moteur
deterministe — et tourne donc meme quand l'agent est coupe ou le budget epuise.
Un garde-fou qui s'arrete en meme temps que ce qu'il surveille ne garde rien.

`--strict` sert a la brancher sur une tache periodique qui doit echouer
bruyamment : un ecart legal qui ne fait rien echouer nulle part finit par
n'etre lu par personne.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from apps.agent import watch


class Command(BaseCommand):
    help = "Recontrole les ratios d'impact et signale les derives. Sans token."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default="ranking_v1")
        parser.add_argument(
            "--strict", action="store_true",
            help="Sort en erreur si une alerte est levee.",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("\n== Veille de l'agent =="))

        precedent = watch.dernier_controle()
        if precedent is not None:
            self.stdout.write(
                f"  dernier controle : {precedent.created_at:%d/%m/%Y %H:%M}"
            )

        controle = watch.veiller(dataset_name=options["dataset"])

        self.stdout.write("")
        for releve in sorted(controle.releves, key=lambda item: item.ratio):
            marque = self.style.ERROR("  sous le seuil") if releve.sous_le_seuil else ""
            self.stdout.write(f"  {releve.dimension:<20} {releve.ratio:.3f}{marque}")

        if controle.premier_releve:
            self.stdout.write(
                self.style.WARNING(
                    "\nPremier releve : aucune derive ne peut etre detectee tant "
                    "qu'il n'y a rien a comparer. C'est le second passage qui "
                    "commence a servir."
                )
            )

        if not controle.alertes:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nAucune alerte. Pire ratio {controle.pire_ratio:.3f}, "
                    f"releve date de ce jour."
                )
            )
            return

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Alertes =="))
        for alerte in controle.alertes:
            style = (
                self.style.ERROR
                if alerte.niveau == "ecart_legal"
                else self.style.WARNING
            )
            self.stdout.write(style(f"  [{alerte.niveau}] {alerte.message}"))

        self.stdout.write(
            "\nLa veille ne bloque rien : elle constate, date et signale. "
            "Corriger reste une decision humaine."
        )
        if options["strict"]:
            sys.exit(1)
