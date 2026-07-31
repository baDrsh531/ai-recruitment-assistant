"""Controle periodique du biais.

    python manage.py monitor_bias
    python manage.py monitor_bias --dry-run    # mesure sans enregistrer
    python manage.py monitor_bias --strict     # sort en erreur sur une alerte

A programmer une fois par semaine. La commande recalcule les ratios d'impact,
les compare au dernier releve enregistre, et journalise le resultat — c'est
l'enregistrement qui permet de repondre a « depuis quand ? », et non le calcul.

`--strict` fait echouer la commande des qu'une alerte apparait : utile dans une
chaine d'integration, ou l'on veut qu'une derive arrete le train. En
exploitation, la commande se contente de constater : un systeme qui refuserait
de scorer parce qu'un ratio a baisse mettrait un recruteur devant un ecran vide
sans qu'il puisse rien y faire.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.evaluation import bias, monitoring


class Command(BaseCommand):
    help = "Recalcule les ratios d'impact, les compare au dernier releve."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default="ranking_v1")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Mesure sans rien enregistrer. Aucun historique n'est constitue.",
        )
        parser.add_argument(
            "--strict", action="store_true",
            help="Sort en erreur des qu'une alerte apparait.",
        )

    def handle(self, *args, **options):
        controle = monitoring.check(
            dataset_name=options["dataset"], record=not options["dry_run"]
        )

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n== Controle de biais ==")
        )
        if controle.premier_releve:
            self.stdout.write(
                "Premier releve : rien a comparer. Les derives seront visibles "
                "a partir du prochain.\n"
            )

        self.stdout.write(f"  {'dimension':<24}{'ratio':>8}{'precedent':>12}{'ecart':>9}")
        for releve in controle.releves:
            precedent = controle.precedents.get(releve.dimension)
            if precedent is None:
                colonnes = f"{'—':>12}{'—':>9}"
            else:
                colonnes = f"{precedent:>12.3f}{releve.ratio - precedent:>+9.3f}"
            marque = " !" if releve.sous_le_seuil else "  "
            self.stdout.write(
                f"  {releve.dimension:<24}{releve.ratio:>8.3f}{colonnes}{marque}"
            )

        self.stdout.write(
            f"\n  Seuil legal : {bias.IMPACT_RATIO_THRESHOLD:.2f} "
            f"(quatre cinquiemes) · derive signalee a partir de "
            f"{monitoring.SEUIL_DERIVE:.2f}"
        )

        if not controle.alertes:
            self.stdout.write(self.style.SUCCESS("\nAucune alerte.\n"))
        for alerte in controle.alertes:
            style = (
                self.style.ERROR if alerte.niveau == "ecart_legal"
                else self.style.WARNING
            )
            self.stdout.write(style(f"\n[{alerte.niveau}] {alerte.message}"))

        if options["dry_run"]:
            self.stdout.write(
                "\nRien n'a ete enregistre : ce releve ne servira pas de point "
                "de comparaison."
            )

        if options["strict"] and controle.alertes:
            raise CommandError(
                f"{len(controle.alertes)} alerte(s) de biais. "
                "Voir la page Transparence pour le detail."
            )
