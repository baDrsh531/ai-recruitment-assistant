"""Mesure la qualite du moteur de classement sur un jeu annote.

    python manage.py evaluate
    python manage.py evaluate --json rapports/2026-07-25.json
    python manage.py evaluate --baseline rapports/reference.json
    python manage.py evaluate --detail

Sans `--baseline`, la commande compare les moyennes aux seuils de
non-regression. Avec, elle affiche l'ecart metrique par metrique : c'est la
seule facon de repondre a « ma modification a-t-elle ameliore quelque chose ? »
autrement que par une impression.

Le code de sortie vaut 1 si un seuil est franchi ou si une metrique regresse :
la commande est directement utilisable en integration continue.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.evaluation import harness

# Une metrique qui perd plus que cela par rapport a la reference est signalee.
REGRESSION_TOLERANCE = 0.01

LABELS = {
    "ndcg_at_5": "nDCG@5",
    "precision_at_3": "P@3",
    "pair_accuracy": "Paires",
    "spearman": "Spearman",
}


class Command(BaseCommand):
    help = "Evalue le moteur de classement sur un jeu de donnees annote."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset", action="append", dest="datasets",
            help="Jeu a evaluer. Repetable. Par defaut : tous.",
        )
        parser.add_argument("--json", dest="json_path", help="Ecrit le rapport en JSON.")
        parser.add_argument(
            "--baseline", help="Rapport JSON de reference, pour mesurer l'ecart."
        )
        parser.add_argument(
            "--detail", action="store_true", help="Affiche le detail par cas."
        )

    def handle(self, *args, **options):
        names = options["datasets"] or harness.available_datasets()
        if not names:
            raise CommandError("Aucun jeu d'evaluation disponible.")

        reports = []
        failed = False

        for name in names:
            try:
                report = harness.run(name)
            except FileNotFoundError as exc:
                raise CommandError(str(exc)) from exc
            reports.append(report)
            failed |= self._render(report, options)

        if options["json_path"]:
            path = Path(options["json_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    reports[0].as_dict() if len(reports) == 1
                    else [report.as_dict() for report in reports],
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.stdout.write(f"\nRapport ecrit dans {path}")

        if options["baseline"]:
            failed |= self._render_comparison(reports[0], Path(options["baseline"]))

        self.stdout.write("")
        if failed:
            self.stdout.write(self.style.ERROR("Evaluation en echec.\n"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Evaluation conforme.\n"))

    # ----------------------------------------------------------------------
    def _render(self, report: harness.Report, options) -> bool:
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n== {report.dataset} v{report.dataset_version} =="
            )
        )
        self.stdout.write(
            f"Moteur {report.engine_version} · "
            + ("semantique active" if report.semantic_used else "ontologie seule")
            + f" · {len(report.cases)} cas"
        )

        header = f"\n{'Cas':<28}" + "".join(f"{LABELS[k]:>10}" for k in LABELS)
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for case in report.cases:
            row = f"{case.id:<28}" + "".join(
                f"{getattr(case, key):>10.3f}" for key in LABELS
            )
            self.stdout.write(row)

        self.stdout.write("-" * len(header))
        self.stdout.write(
            f"{'Moyenne':<28}"
            + "".join(f"{report.aggregate[key]:>10.3f}" for key in LABELS)
        )

        if options["detail"]:
            self._render_detail(report)

        failures = report.failures()
        self.stdout.write("\nSeuils de non-regression :")
        for name, threshold in harness.THRESHOLDS.items():
            value = report.aggregate.get(name, 0.0)
            mark = (
                self.style.ERROR("ECHEC")
                if name in failures
                else self.style.SUCCESS("ok   ")
            )
            self.stdout.write(f"  {mark}  {LABELS[name]:<10} {value:.3f} >= {threshold}")
        return bool(failures)

    def _render_detail(self, report: harness.Report) -> None:
        for case in report.cases:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n-- {case.id} --"))
            self.stdout.write(f"{'Rg':<4}{'Candidat':<26}{'Score':>8}{'Pertinence':>12}")
            ideal = sorted(case.relevances, reverse=True)
            for rank, (identifier, relevance, value) in enumerate(
                zip(case.predicted_order, case.relevances, case.scores, strict=True),
                start=1,
            ):
                flag = "" if relevance == ideal[rank - 1] else "  <- ordre discutable"
                self.stdout.write(
                    f"{rank:<4}{identifier:<26}{value:>8.3f}{relevance:>12}{flag}"
                )

    def _render_comparison(self, report: harness.Report, path: Path) -> bool:
        if not path.is_file():
            raise CommandError(f"Rapport de reference introuvable : {path}")

        baseline = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(baseline, list):
            baseline = next(
                (item for item in baseline if item["dataset"] == report.dataset), {}
            )

        deltas = harness.compare(report, baseline)
        if not deltas:
            self.stdout.write(
                self.style.WARNING("\nAucune metrique comparable dans la reference.")
            )
            return False

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Ecart avec la reference =="))
        self.stdout.write(
            f"Reference : moteur {baseline.get('engine_version', '?')}, "
            f"jeu v{baseline.get('dataset_version', '?')}"
        )
        self.stdout.write(f"\n{'Metrique':<12}{'Reference':>12}{'Actuel':>10}{'Ecart':>10}")

        regressed = False
        for name, values in deltas.items():
            delta = values["delta"]
            if delta < -REGRESSION_TOLERANCE:
                rendered, regressed = self.style.ERROR(f"{delta:+.3f}"), True
            elif delta > REGRESSION_TOLERANCE:
                rendered = self.style.SUCCESS(f"{delta:+.3f}")
            else:
                rendered = f"{delta:+.3f}"
            self.stdout.write(
                f"{LABELS.get(name, name):<12}{values['baseline']:>12.3f}"
                f"{values['current']:>10.3f}{rendered:>10}"
            )

        if regressed:
            self.stdout.write(
                self.style.ERROR(
                    f"\nRegression detectee (tolerance {REGRESSION_TOLERANCE})."
                )
            )
        return regressed
