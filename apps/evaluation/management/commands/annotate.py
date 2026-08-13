"""Faire annoter un jeu par quelqu'un d'autre, et mesurer l'accord.

    python manage.py annotate --export annotation.csv
    python manage.py annotate --comparer annotation-remplie.csv

La limite la plus serieuse des jeux annotes de ce projet n'est pas leur taille,
c'est qu'une seule personne les a ecrits. Cette commande n'en fait pas
apparaitre une seconde ; elle enleve ce qui l'empechait d'exister — jusqu'ici,
meme un volontaire n'aurait rien eu a annoter, les cas vivant dans un JSON de
deux mille lignes melant offres, candidats et notes deja posees.

L'export ne montre jamais les notes existantes : les montrer transformerait le
second annotateur en relecteur du premier, et l'accord mesure ne vaudrait plus
rien.

Aucun appel au modele.
"""

from __future__ import annotations

import pathlib

from django.core.management.base import BaseCommand, CommandError

from apps.evaluation import annotation, harness


class Command(BaseCommand):
    help = "Exporte un jeu a annoter, ou compare deux annotations. Sans token."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default="ranking_v1")
        parser.add_argument(
            "--export", help="Ecrit le CSV a faire remplir, colonne vide."
        )
        parser.add_argument(
            "--comparer", help="CSV rempli par un second annotateur."
        )

    def handle(self, *args, **options):
        if not options["export"] and not options["comparer"]:
            raise CommandError("Choisir --export ou --comparer.")

        dataset = harness.load_dataset(options["dataset"])

        if options["export"]:
            self._exporter(dataset, pathlib.Path(options["export"]))
        if options["comparer"]:
            self._comparer(dataset, pathlib.Path(options["comparer"]))

    # --- Export ---------------------------------------------------------------
    def _exporter(self, dataset: dict, chemin: pathlib.Path) -> None:
        contenu = annotation.exporter(dataset)
        chemin.parent.mkdir(parents=True, exist_ok=True)
        # BOM : sans lui, Excel ouvre l'UTF-8 en latin-1 et abime les accents
        # du premier coup d'oeil — ce qui suffit a decourager un annotateur.
        chemin.write_text(contenu, encoding="utf-8-sig")

        lignes = contenu.count("\n") - 1
        self.stdout.write(self.style.MIGRATE_HEADING("\n== A faire annoter =="))
        self.stdout.write(f"  fichier    {chemin}")
        self.stdout.write(f"  profils    {lignes}")
        self.stdout.write("\n  Bareme a transmettre avec le fichier :")
        for note, libelle in sorted(annotation.NIVEAUX.items(), reverse=True):
            self.stdout.write(f"    {note}  {libelle}")
        self.stdout.write(
            "\n  La colonne « pertinence » est vide, et c'est voulu : montrer "
            "les notes\n  existantes ferait du second annotateur un relecteur "
            "du premier."
            "\n\n  Au retour :  python manage.py annotate --comparer <fichier>"
        )

    # --- Comparaison ----------------------------------------------------------
    def _comparer(self, dataset: dict, chemin: pathlib.Path) -> None:
        if not chemin.is_file():
            raise CommandError(f"Fichier introuvable : {chemin}")

        try:
            accord = annotation.comparer(dataset, chemin.read_text(encoding="utf-8-sig"))
        except ValueError as erreur:
            raise CommandError(str(erreur)) from erreur

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Accord entre annotateurs =="))
        self.stdout.write(f"  profils communs   {accord.communs}")
        self.stdout.write(f"  memes notes       {accord.identiques}")
        self.stdout.write(f"  accord brut       {accord.accord_brut:.0%}")
        if accord.kappa is not None:
            self.stdout.write(f"  kappa de Cohen    {accord.kappa:.2f}")

        if accord.manquants:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  {len(accord.manquants)} profil(s) du jeu non annotes."
                )
            )
        if accord.inconnus:
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(accord.inconnus)} ligne(s) sans correspondance dans "
                    f"le jeu — identifiants modifies ?"
                )
            )

        self.stdout.write(f"\n  {accord.lecture}")

        if accord.ecarts:
            self.stdout.write(self.style.MIGRATE_HEADING("\n== Ou ils divergent =="))
            self.stdout.write(f"  {'profil':<44}{'jeu':>5}{'second':>8}")
            for cle, reference, seconde in accord.ecarts[:25]:
                style = self.style.ERROR if abs(reference - seconde) > 1 else str
                self.stdout.write(style(f"  {cle:<44}{reference:>5}{seconde:>8}"))
            reste = len(accord.ecarts) - 25
            if reste > 0:
                self.stdout.write(f"  ... et {reste} autre(s).")
            self.stdout.write(
                "\n  Un ecart n'est pas une erreur : deux recruteurs ne classent "
                "pas pareil.\n  C'est precisement ce que le kappa chiffre, et ce "
                "que le jeu ne disait pas."
            )

        if accord.suffisant and accord.kappa is not None:
            self.stdout.write(
                "\n  A reporter dans la provenance du jeu :"
                f'\n    "annotateurs": 2,'
                f'\n    "accord_inter_annotateur": {accord.kappa:.2f}'
            )
