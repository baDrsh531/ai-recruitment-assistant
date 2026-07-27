"""Mesure la qualite de l'extraction des CV.

    python manage.py evaluate_extraction
    python manage.py evaluate_extraction --detail --json rapports/extraction.json

Contrairement a `evaluate`, cette commande **appelle reellement le modele** :
elle demande un serveur d'inference joignable et ne tourne pas en integration
continue. Comptez une dizaine de secondes par CV.

La verite terrain n'est pas annotee a la main : chaque CV est genere a partir
d'un profil structure, et l'extraction est comparee a ce profil.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.evaluation import extraction

LABELS = {
    "identity_accuracy": "Identite",
    "skills_f1": "Competences F1",
    "skills_precision": "  precision",
    "skills_recall": "  rappel",
    "languages_f1": "Langues F1",
    "evidence_anchored": "Preuves ancrees",
    "experience_years_mae": "Erreur anciennete",
    "seconds_mean": "Duree moyenne",
}


class Command(BaseCommand):
    help = "Evalue l'extraction des CV sur un jeu genere a verite terrain connue."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default="extraction_v1")
        parser.add_argument("--json", dest="json_path")
        parser.add_argument(
            "--detail", action="store_true",
            help="Affiche les competences manquees et inventees, cas par cas.",
        )

    def handle(self, *args, **options):
        try:
            report = extraction.run(options["dataset"])
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n== Extraction — {report.dataset} v{report.dataset_version} =="
            )
        )
        self.stdout.write(
            "Verite terrain connue par construction. Les CV generes sont plus "
            "propres que\nles vrais : ces scores sont optimistes."
        )

        header = (
            f"\n{'Cas':<24}{'Mise en page':<15}{'Voie':<10}"
            f"{'Identite':>9}{'Comp. F1':>10}{'Langues':>9}{'Preuves':>9}{'Duree':>8}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for case in report.cases:
            if case.error:
                self.stdout.write(
                    f"{case.id:<24}{case.layout:<15}"
                    + self.style.ERROR(f"ECHEC — {case.error[:60]}")
                )
                continue
            preuves = (
                f"{case.evidence_anchored:>4}/{case.evidence_total:<4}"
                if case.evidence_verifiable
                else f"{'n/a':>4}     "
            )
            self.stdout.write(
                f"{case.id:<24}{case.layout:<15}{case.method:<10}"
                f"{case.identity_accuracy:>9.2f}{case.skills['f1']:>10.2f}"
                f"{case.languages['f1']:>9.2f}{preuves}{case.seconds:>7.1f}s"
            )

        unverifiable = [c for c in report.cases if not c.error and not c.evidence_verifiable]
        if unverifiable:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  n/a : {len(unverifiable)} document(s) sans couche texte. "
                    "Les citations du modele ne peuvent pas y etre confrontees ;\n"
                    "        elles ne sont pas contredites, elles sont inverifiables."
                )
            )

        if not report.aggregate:
            raise CommandError("Aucun cas exploitable : le serveur repond-il ?")

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Moyennes =="))
        for name, label in LABELS.items():
            if name not in report.aggregate:
                continue
            value = report.aggregate[name]
            suffix = " s" if name == "seconds_mean" else ""
            unit = " an(s)" if name == "experience_years_mae" else suffix
            self.stdout.write(f"  {label:<20}{value:>8.3f}{unit}")

        if options["detail"]:
            self._render_detail(report)

        failures = report.failures()
        self.stdout.write(self.style.MIGRATE_HEADING("\n== Seuils =="))
        for name, threshold in extraction.THRESHOLDS.items():
            value = report.aggregate.get(name)
            if value is None:
                continue
            sense = "<=" if name in extraction.LOWER_IS_BETTER else ">="
            mark = (
                self.style.ERROR("ECHEC")
                if name in failures
                else self.style.SUCCESS("ok   ")
            )
            self.stdout.write(
                f"  {mark}  {LABELS.get(name, name):<20}{value:.3f} {sense} {threshold}"
            )

        if options["json_path"]:
            path = Path(options["json_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report.as_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.stdout.write(f"\nRapport ecrit dans {path}")

        self.stdout.write("")
        if failures:
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Extraction conforme aux seuils.\n"))

    # ----------------------------------------------------------------------
    def _render_detail(self, report: extraction.Report) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("\n== Detail par cas =="))
        for case in report.cases:
            if case.error:
                continue
            self.stdout.write(f"\n{case.id} ({case.layout})")
            manquants = [
                champ for champ, correct in case.identity_detail.items() if not correct
            ]
            if manquants:
                self.stdout.write(
                    self.style.WARNING(f"  identite incorrecte : {', '.join(manquants)}")
                )
            if case.missed_skills:
                self.stdout.write(
                    self.style.WARNING(f"  competences manquees : {', '.join(case.missed_skills)}")
                )
            if case.invented_skills:
                # Une competence « inventee » est souvent une competence
                # deduite d'une experience : a lire avant de conclure.
                self.stdout.write(
                    f"  competences en trop  : {', '.join(case.invented_skills)}"
                )
            self.stdout.write(
                f"  experiences {case.experiences_found}/{case.experiences_expected}"
                f" · formations {case.education_found}/{case.education_expected}"
                f" · anciennete {case.experience_years_found} vs "
                f"{case.experience_years_expected} attendue"
            )
