"""Signale les CV de candidats differents dont le texte se recouvre.

    python manage.py check_plagiarism
    python manage.py check_plagiarism --seuil 0.5

A ne pas confondre avec la page « Doublons », qui cherche une meme personne
sous deux dossiers. Ici les candidats sont differents et le texte se ressemble.

Aucun appel au modele : la mesure est deterministe, et ne coute que du calcul.

**Cette commande n'accuse personne.** Un fort recouvrement peut venir d'une
copie comme d'un modele partage dans une promotion. Elle produit une liste a
regarder ; un humain tranche.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.candidates import plagiarism


class Command(BaseCommand):
    help = "Compare les CV et signale ceux qui se recouvrent. Sans token."

    def add_arguments(self, parser):
        parser.add_argument(
            "--seuil", type=float, default=plagiarism.SEUIL_SIGNALEMENT
        )
        parser.add_argument("--afficher", type=int, default=20)

    def handle(self, *args, **options):
        rapport = plagiarism.analyser(seuil=options["seuil"])

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Comparaison des CV =="))
        self.stdout.write(f"  compares            {rapport.documents_compares}")
        self.stdout.write(
            f"  ignores             {rapport.documents_ignores} "
            f"(texte trop court pour que la mesure signifie quelque chose)"
        )
        self.stdout.write(
            f"  formules retirees   {rapport.empreintes_retirees} "
            f"(presentes dans plus de "
            f"{round(plagiarism.PART_TROP_COMMUNE * 100)} % des CV)"
        )
        self.stdout.write(f"  seuil               {options['seuil']:.2f}")

        self.stdout.write(f"\n  {rapport.lecture}")

        if not rapport.paires:
            return

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Paires signalees =="))
        for paire in rapport.paires[: options["afficher"]]:
            gauche, droite = paire.candidats
            style = (
                self.style.ERROR
                if paire.similarite >= 0.80
                else self.style.WARNING
            )
            self.stdout.write(
                style(f"  {paire.pourcentage:>3} %  {gauche} / {droite}")
            )
            self.stdout.write(f"        {paire.gravite}")
            if paire.extrait:
                self.stdout.write(f"        « {paire.extrait} »")

        reste = len(rapport.paires) - options["afficher"]
        if reste > 0:
            self.stdout.write(f"\n  ... et {reste} autre(s), non affichees.")
