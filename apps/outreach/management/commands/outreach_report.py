"""Etat des echanges avec les candidats, et surtout de ceux qui n'ont pas eu lieu.

    python manage.py outreach_report
    python manage.py outreach_report --strict   # sort en erreur s'il reste un oubli

Aucun appel au modele : le calcul ne lit que des dates. `--strict` sert a la
brancher sur une tache periodique — un dossier ecarte sans reponse depuis trois
semaines doit faire echouer quelque chose quelque part, sinon personne ne le
verra jamais.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from apps.outreach import silence
from apps.outreach.backends import etat_du_canal
from apps.outreach.models import Channel


class Command(BaseCommand):
    help = "Compte les candidats laisses sans reponse. Sans token."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=15)
        parser.add_argument(
            "--strict", action="store_true",
            help="Sort en erreur s'il reste au moins un dossier sans reponse.",
        )

    def handle(self, *args, **options):
        mesure = silence.mesurer()

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Canaux =="))
        LIBELLES_ETAT = {
            "connecte": self.style.SUCCESS("connecte"),
            "connectable": "modelise, non connecte",
            "hors_logiciel": "hors logiciel, se consigne",
        }
        for canal, libelle in Channel.choices:
            if canal == Channel.OTHER:
                continue
            self.stdout.write(f"  {libelle:<22} {LIBELLES_ETAT[etat_du_canal(canal)]}")

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Ce qu'on n'a pas dit =="))
        self.stdout.write(
            f"  ecartes                {mesure.ecartes} "
            f"({mesure.ecartes_prevenus} prevenus, "
            f"{mesure.ecartes_sans_reponse} sans reponse)"
        )
        self.stdout.write(
            f"  ouverts > {silence.JOURS_AVANT_SILENCE} jours    "
            f"{mesure.ouverts_anciens} "
            f"(dont {mesure.ouverts_sans_message} sans un seul message)"
        )
        if mesure.delai_median_jours is not None:
            self.stdout.write(
                f"  delai median           {mesure.delai_median_jours} jours "
                f"entre la decision et la notification"
            )

        self.stdout.write(f"\n  {mesure.lecture}")

        if not mesure.oublis:
            self.stdout.write(
                self.style.SUCCESS(
                    "\nAucun dossier sans reponse. C'est l'etat normal, pas un "
                    "exploit — c'est simplement rare."
                )
            )
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n== Dossiers a reprendre, le plus ancien d'abord ==")
        )
        for oubli in mesure.oublis[: options["limit"]]:
            cause = (
                "ecarte, jamais prevenu"
                if oubli.apres_decision
                else "ouvert, aucun message"
            )
            self.stdout.write(
                self.style.WARNING(
                    f"  {oubli.jours:>4} j  {oubli.candidat:<26} {cause}"
                )
            )

        reste = len(mesure.oublis) - options["limit"]
        if reste > 0:
            self.stdout.write(f"  ... et {reste} autre(s), non affiches (--limit).")

        if options["strict"]:
            sys.exit(1)
