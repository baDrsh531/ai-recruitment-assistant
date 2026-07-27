"""Audit de biais du moteur de classement.

    python manage.py audit_bias
    python manage.py audit_bias --json rapports/biais.json

Repond a la question de l'auditeur : si cette meme personne avait un autre
prenom, une autre ville, un autre age apparent, serait-elle toujours retenue ?

Le code de sortie vaut 1 si un ratio d'impact passe sous 0.80 (regle des
quatre cinquiemes) ou si une propriete de non-discrimination est mise en
defaut : l'audit est directement utilisable en integration continue.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.evaluation import bias


class Command(BaseCommand):
    help = "Mesure l'effet des attributs identitaires sur le classement."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default="ranking_v1")
        parser.add_argument("--json", dest="json_path", help="Ecrit le rapport en JSON.")
        parser.add_argument(
            "--detail", action="store_true",
            help="Affiche le taux de selection de chaque variante.",
        )
        parser.add_argument(
            "--blind", action="store_true",
            help="Audite le moteur en screening a l'aveugle.",
        )
        parser.add_argument(
            "--compare-blind", action="store_true", dest="compare_blind",
            help="Compare les deux modes et chiffre l'effet de l'attenuation.",
        )

    def handle(self, *args, **options):
        if options["compare_blind"]:
            return self._compare(options)

        report = bias.audit(options["dataset"], blind=options["blind"])
        if report.blind:
            self.stdout.write(
                self.style.WARNING("\nMode : screening a l'aveugle (localisation exclue)")
            )

        self.stdout.write(
            self.style.MIGRATE_HEADING(f"\n== Audit de biais — {report.dataset} ==")
        )
        self.stdout.write(
            f"Moteur {report.engine_version} · "
            f"selection = {report.shortlist_size} premiers · "
            f"seuil du ratio d'impact {bias.IMPACT_RATIO_THRESHOLD}"
        )

        header = (
            f"\n{'Attribut':<20}{'Ecart moyen':>13}{'Ecart max':>11}"
            f"{'Rangs modifies':>16}{'Ratio impact':>14}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for item in report.dimensions:
            ratio = (
                self.style.ERROR(f"{item.impact_ratio:>14.3f}")
                if not item.passes
                else f"{item.impact_ratio:>14.3f}"
            )
            self.stdout.write(
                f"{item.dimension:<20}{item.mean_abs_delta:>13.5f}"
                f"{item.max_abs_delta:>11.5f}"
                f"{item.rank_changes:>10} / {item.comparisons:<3}{ratio}"
            )

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Lecture =="))
        for item in report.dimensions:
            if not item.influences_score:
                self.stdout.write(
                    f"  {item.dimension:<20} "
                    + self.style.SUCCESS("aucun effet mesurable sur le score")
                )
            else:
                self.stdout.write(
                    f"  {item.dimension:<20} influe sur le score "
                    f"(exemple : {item.max_delta_example})"
                )

        if options["detail"]:
            self.stdout.write(
                self.style.MIGRATE_HEADING("\n== Taux de selection par variante ==")
            )
            for item in report.dimensions:
                self.stdout.write(f"\n{item.dimension}")
                for label, rate in item.selection_rates.items():
                    self.stdout.write(f"  {label:<32}{rate:>8.3f}")

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n== Proprietes de non-discrimination ==")
        )
        for check in report.properties:
            mark = (
                self.style.SUCCESS("verifiee")
                if check.holds
                else self.style.ERROR("MISE EN DEFAUT")
            )
            self.stdout.write(f"  {mark}  {check.name}")
            self.stdout.write(f"            {check.description}")
            if check.detail:
                self.stdout.write(self.style.WARNING(f"            {check.detail}"))

        if options["json_path"]:
            path = Path(options["json_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report.as_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.stdout.write(f"\nRapport ecrit dans {path}")

        failures = report.failures()
        broken = report.broken_properties()

        self.stdout.write("")
        if failures or broken:
            for item in failures:
                self.stdout.write(
                    self.style.ERROR(
                        f"Ratio d'impact insuffisant sur « {item.dimension} » : "
                        f"{item.impact_ratio} < {bias.IMPACT_RATIO_THRESHOLD}"
                    )
                )
            for check in broken:
                self.stdout.write(
                    self.style.ERROR(f"Propriete mise en defaut : {check.name}")
                )
            raise SystemExit(1)

        self.stdout.write(
            self.style.SUCCESS(
                "Audit conforme sur les attributs testes.\n"
                "Rappel : cet audit ne prouve pas l'absence de biais. Il mesure "
                "l'effet des attributs qu'il teste, sur le jeu qu'on lui donne.\n"
            )
        )

    # ----------------------------------------------------------------------
    def _compare(self, options) -> None:
        standard, blind, mitigations = bias.compare_blind(options["dataset"])

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n== Effet du screening a l'aveugle — {standard.dataset} =="
            )
        )
        self.stdout.write(
            f"Moteur {standard.engine_version} · le mode aveugle exclut la "
            "localisation du calcul et masque les employeurs dans l'analyse."
        )

        header = (
            f"\n{'Attribut':<20}{'Ratio standard':>16}{'Ratio aveugle':>15}"
            f"{'Gain':>9}{'Rangs modifies':>18}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for item in mitigations:
            gain = (
                self.style.SUCCESS(f"{item.gain:>+9.3f}")
                if item.gain > 0
                else f"{item.gain:>+9.3f}"
            )
            self.stdout.write(
                f"{item.dimension:<20}{item.ratio_standard:>16.3f}"
                f"{item.ratio_blind:>15.3f}{gain}"
                f"{item.rank_changes_standard:>12} -> {item.rank_changes_blind:<4}"
            )

        neutralised = [item for item in mitigations if item.neutralised]
        self.stdout.write(self.style.MIGRATE_HEADING("\n== Lecture =="))
        if neutralised:
            for item in neutralised:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  {item.dimension} : effet neutralise "
                        f"({item.max_delta_standard:.5f} -> {item.max_delta_blind:.5f}), "
                        f"ratio d'impact {item.ratio_standard:.3f} -> {item.ratio_blind:.3f}"
                    )
                )
        else:
            self.stdout.write("  Le mode aveugle ne modifie aucun ratio d'impact.")

        self.stdout.write(
            "\n  Contrepartie : la contrainte geographique disparait du calcul. "
            "Pour un poste sur site,\n  elle devra etre reintroduite plus tard "
            "dans le processus, par une decision humaine\n  tracee, et non par "
            "un tri automatique."
        )
        self.stdout.write("")
