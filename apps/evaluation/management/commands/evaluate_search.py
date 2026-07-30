"""Mesure la recherche plein texte.

    python manage.py evaluate_search
    python manage.py evaluate_search --detail
    python manage.py evaluate_search --compare   # lexical seul contre hybride

La commande n'appelle aucun modele de langage : BM25 est du calcul. Elle peut
solliciter la couche d'embeddings si `EMBEDDING_PROVIDER` l'active, auquel cas
`--compare` dit ce que la fusion apporte — ou coute.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.evaluation import search_eval

LABELS = {
    "recall_at_5": "Rappel@5",
    "recall_at_5_ceiling": "  plafond atteignable",
    "mrr": "MRR",
    "precision_at_3": "Precision@3",
    "empty_queries_handled": "Requetes sans reponse traitees",
}


class Command(BaseCommand):
    help = "Evalue la recherche plein texte sur un jeu a pertinence connue."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default="search_v1")
        parser.add_argument("--json", dest="json_path")
        parser.add_argument(
            "--detail", action="store_true",
            help="Affiche, requete par requete, l'ordre obtenu et les profils manques.",
        )
        parser.add_argument(
            "--compare", action="store_true",
            help="Joue le lexical seul puis l'hybride, et affiche l'ecart.",
        )
        parser.add_argument(
            "--lexical", action="store_true",
            help="Force le lexical seul, sans couche vectorielle.",
        )

    def handle(self, *args, **options):
        try:
            if options["compare"]:
                return self._comparer(options)
            rapport = search_eval.run(
                options["dataset"], hybrid=not options["lexical"]
            )
        except (FileNotFoundError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self._afficher(rapport, detail=options["detail"])

        if options["json_path"]:
            chemin = Path(options["json_path"])
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_text(
                json.dumps(rapport.as_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.stdout.write(f"\nRapport ecrit dans {chemin}")

        echecs = rapport.failures()
        if echecs:
            details = ", ".join(
                f"{LABELS.get(nom, nom)} {obtenu:.3f} < {seuil:.2f}"
                for nom, (obtenu, seuil) in echecs.items()
            )
            raise CommandError(f"Sous les seuils de non-regression : {details}")
        self.stdout.write(self.style.SUCCESS("\nTous les seuils sont tenus."))

    # ------------------------------------------------------------------
    def _afficher(self, rapport, *, detail: bool) -> None:
        mode = "hybride" if rapport.hybrid else "lexical seul"
        couche = (
            "BM25 + vectoriel" if rapport.semantic_used else "BM25 seul"
        )
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n== Recherche — {rapport.dataset} v{rapport.dataset_version} =="
            )
        )
        self.stdout.write(f"Mode demande : {mode} · couche effective : {couche}\n")

        for nom, valeur in rapport.aggregate.items():
            self.stdout.write(f"  {LABELS.get(nom, nom):<32}{valeur:>7.3f}")

        if not rapport.semantic_used and rapport.hybrid:
            self.stdout.write(
                "\n  Couche vectorielle indisponible : l'hybride se ramene au "
                "lexical. Voir EMBEDDING_PROVIDER et `probe_semantic`."
            )

        if not detail:
            return

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Requete par requete =="))
        for item in rapport.queries:
            self.stdout.write(f"\n  « {item.query} »")
            if item.expects_nothing:
                etat = "vide, comme attendu" if item.answered_nothing else (
                    f"NON VIDE : {item.returned[:3]}"
                )
                self.stdout.write(f"    reponse attendue vide -> {etat}")
                continue
            plafond = (
                "" if item.recall_at_5_ceiling >= 1.0
                else f" (plafond {item.recall_at_5_ceiling:.2f})"
            )
            self.stdout.write(
                f"    rappel@5 {item.recall_at_5:.2f}{plafond} · "
                f"precision@3 {item.precision_at_3:.2f} · "
                f"1/rang {item.reciprocal_rank:.2f}"
            )
            self.stdout.write(f"    obtenu   : {item.returned[:5] or 'rien'}")
            self.stdout.write(f"    attendu  : {item.expected}")
            if item.missed and not item.at_ceiling:
                self.stdout.write(
                    self.style.WARNING(f"    manques  : {item.missed}")
                )
            elif item.missed:
                self.stdout.write(
                    f"    hors des 5 places, plafond atteint : {item.missed}"
                )

    def _comparer(self, options) -> None:
        lexical = search_eval.run(options["dataset"], hybrid=False)
        hybride = search_eval.run(options["dataset"], hybrid=True)

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n== Lexical seul contre hybride ==")
        )
        if not hybride.semantic_used:
            self.stdout.write(
                self.style.WARNING(
                    "La couche vectorielle est indisponible : les deux colonnes "
                    "sont identiques par construction, la comparaison ne dit rien."
                )
            )

        self.stdout.write(f"\n  {'metrique':<32}{'lexical':>9}{'hybride':>9}{'ecart':>9}")
        for nom in lexical.aggregate:
            avant = lexical.aggregate[nom]
            apres = hybride.aggregate.get(nom, 0.0)
            self.stdout.write(
                f"  {LABELS.get(nom, nom):<32}{avant:>9.3f}{apres:>9.3f}"
                f"{apres - avant:>+9.3f}"
            )
