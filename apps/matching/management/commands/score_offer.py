"""Score toutes les candidatures d'une offre et affiche le classement.

    python manage.py score_offer ingenieur-backend-python-ia
    python manage.py score_offer <slug> --no-explain   # sans appel au modele

`--no-explain` n'utilise que le moteur deterministe : aucun serveur
d'inference requis, resultat identique a chaque execution.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.jobs.models import JobOffer
from apps.matching.services import latest_scores, score_offer


class Command(BaseCommand):
    help = "Calcule le score de compatibilite de toutes les candidatures d'une offre."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Slug de l'offre.")
        parser.add_argument(
            "--no-explain",
            action="store_true",
            help="Score seul, sans analyse redigee par le modele.",
        )
        parser.add_argument(
            "--detail", action="store_true", help="Affiche le detail par critere."
        )

    def handle(self, *args, **options):
        offer = JobOffer.objects.filter(slug=options["slug"]).first()
        if offer is None:
            available = ", ".join(JobOffer.objects.values_list("slug", flat=True)[:10])
            raise CommandError(f"Offre introuvable. Slugs disponibles : {available}")

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n== {offer.title} =="))
        self.stdout.write(f"Competences obligatoires : {offer.required_skills.count()}")
        self.stdout.write(f"Anciennete minimale      : {offer.experience_min_years} ans")

        scores = score_offer(offer, with_explanation=not options["no_explain"])
        if not scores:
            self.stdout.write(self.style.WARNING("\nAucune candidature a scorer.\n"))
            return

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Classement =="))
        self.stdout.write(f"{'Rg':<4}{'Candidat':<28}{'Score':>7}   Ecarts")
        self.stdout.write("-" * 78)

        for rank, score in enumerate(latest_scores(offer), start=1):
            gaps = ", ".join(gap["skill"] for gap in score.gaps) or "-"
            name = score.application.candidate.full_name[:26]
            self.stdout.write(f"{rank:<4}{name:<28}{score.percentage:>6} %   {gaps}")

        timings = sorted(score.compute_ms for score in scores)
        self.stdout.write("")
        self.stdout.write(f"Moteur           {scores[0].engine_version}")
        self.stdout.write(
            "Semantique       "
            + (
                "disponible"
                if scores[0].semantic_used
                else "indisponible (ontologie seule)"
            )
        )
        # La premiere candidature absorbe le chargement du modele d'embeddings ;
        # afficher sa seule latence donnerait une image fausse du regime etabli.
        self.stdout.write(
            f"Calcul           {sum(timings)} ms au total · "
            f"median {timings[len(timings) // 2]} ms · max {timings[-1]} ms"
        )

        if options["detail"]:
            for score in latest_scores(offer):
                self.stdout.write(
                    self.style.MIGRATE_HEADING(
                        f"\n-- {score.application.candidate.full_name} "
                        f"({score.percentage} %) --"
                    )
                )
                for criterion in score.criteria:
                    if criterion["applicable"]:
                        weight = score.weights_used.get(criterion["name"], 0)
                        self.stdout.write(
                            f"  {criterion['label']:<16}"
                            f"{criterion['score'] * 100:>5.0f} %  "
                            f"(poids {weight * 100:.0f} %)"
                        )
                    else:
                        reason = criterion["detail"].get("reason", "")
                        self.stdout.write(
                            f"  {criterion['label']:<16}    -    non applicable ({reason})"
                        )
                for match in score.skill_matches:
                    target = match["matched_with"] or "aucune correspondance"
                    self.stdout.write(
                        f"    · {match['required']:<22}{match['score'] * 100:>5.0f} %  "
                        f"{target} [{match['method']}]"
                    )
                if score.explanation:
                    self.stdout.write("\n  Analyse :")
                    self.stdout.write(f"  {score.explanation[:600]}")

        self.stdout.write(self.style.SUCCESS("\nTermine.\n"))
