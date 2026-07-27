"""Mesure ce que le rapprochement semantique sait reellement rapprocher.

    python manage.py probe_semantic

Cette commande existe pour rendre verifiable une affirmation du README : sur
des intitules techniques courts, un modele de phrases generaliste n'apporte
rien, et peut nuire. Elle affiche la similarite cosinus de paires choisies —
des paires reellement proches, et des paires sans aucun rapport.

Le resultat determine si le rapprochement semantique merite d'etre active.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.ai import embeddings
from apps.matching import engine, ontology

# (offre, candidat, relation attendue par un humain)
PAIRES = [
    ("Symfony", "Symfony", "identiques"),
    ("Symfony", "Laravel", "proches"),
    ("Doctrine ORM", "Eloquent ORM", "proches"),
    ("Twig", "Blade", "proches"),
    ("Polars", "Pandas", "proches"),
    ("Svelte", "React", "proches"),
    ("Rust", "Go", "proches"),
    ("Power BI", "Tableau", "proches"),
    ("Gestion de projet agile", "Scrum", "proches"),
    ("Comptabilite analytique", "Controle de gestion", "proches"),
    ("Symfony", "Comptabilite", "sans rapport"),
    ("Twig", "Soudure", "sans rapport"),
    ("Kubernetes", "Boulangerie", "sans rapport"),
]


class Command(BaseCommand):
    help = "Mesure la similarite semantique sur des paires de reference."

    def handle(self, *args, **options):
        embedder = embeddings.get_embedder_or_none(force=True)
        if embedder is None:
            raise CommandError(
                "Aucun fournisseur d'embeddings disponible. Installe-le avec :\n"
                '    pip install -e ".[local-embeddings]"'
            )

        gauche = [a for a, _, _ in PAIRES]
        droite = [b for _, b, _ in PAIRES]
        vecteurs = embedder.encode(gauche + droite)
        moitie = len(PAIRES)

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n== Rapprochement semantique — paires de reference ==")
        )
        self.stdout.write(
            f"Modele : {embeddings.settings.EMBEDDING['MODEL']}\n"
            f"Seuil de prise en compte : {engine.SEMANTIC_FLOOR}"
        )

        entete = f"\n{'Offre':<26}{'Candidat':<24}{'Attendu':<14}{'cosinus':>9}{'retenu':>9}"
        self.stdout.write(entete)
        self.stdout.write("-" * len(entete))

        proches: list[float] = []
        etrangeres: list[float] = []

        for index, (a, b, attendu) in enumerate(PAIRES):
            cosinus = float(vecteurs[index] @ vecteurs[moitie + index])
            retenu = max(ontology.relatedness(a, b), _semantic_score(cosinus))

            if attendu == "proches":
                proches.append(cosinus)
            elif attendu == "sans rapport":
                etrangeres.append(cosinus)

            ligne = (
                f"{a:<26}{b:<24}{attendu:<14}{cosinus:>9.3f}{retenu:>9.2f}"
            )
            if attendu == "sans rapport" and proches and cosinus > min(proches):
                self.stdout.write(self.style.ERROR(ligne))
            else:
                self.stdout.write(ligne)

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Lecture =="))
        if proches and etrangeres:
            pire_proche = min(proches)
            meilleure_etrangere = max(etrangeres)
            self.stdout.write(
                f"  paire proche la moins bien notee   : {pire_proche:.3f}\n"
                f"  paire sans rapport la mieux notee  : {meilleure_etrangere:.3f}"
            )
            if meilleure_etrangere >= pire_proche:
                self.stdout.write(
                    self.style.ERROR(
                        "\n  Les deux populations se chevauchent : aucun seuil ne peut "
                        "separer\n  les paires proches des paires sans rapport. Sur des "
                        "intitules techniques\n  courts, ce modele n'apporte pas "
                        "d'information exploitable."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        "\n  Les deux populations sont separables : un seuil situe entre "
                        f"{meilleure_etrangere:.2f} et {pire_proche:.2f} est defendable."
                    )
                )
        self.stdout.write("")


def _semantic_score(cosinus: float) -> float:
    if cosinus <= engine.SEMANTIC_FLOOR:
        return 0.0
    ecart = (cosinus - engine.SEMANTIC_FLOOR) / (1 - engine.SEMANTIC_FLOOR)
    return min(engine.SEMANTIC_CEILING * ecart, engine.SEMANTIC_CEILING)
