"""Fabrique les messages types et les fait partir — ou les depose sur le disque.

    python manage.py outreach_selftest --to moi@example.com
    python manage.py outreach_selftest --to moi@example.com --dossier .\\courriers

Sert a repondre a une question simple : **est-ce que ca marche vraiment ?**

Sans `EMAIL_HOST` dans le `.env`, rien ne peut partir : la commande ecrit alors
les messages complets en `.eml`, ouvrables dans Gmail, Outlook ou Thunderbird.
Ce fichier contient le message tel qu'il serait recu — les deux versions texte
et HTML, la marque en piece jointe liee, les en-tetes. Tout est eprouve sauf le
saut SMTP lui-meme.

Ecrire un `.eml` plutot que d'annoncer « envoye » sans serveur suit la meme
regle que le reste du module : on ne simule pas un envoi.
"""

from __future__ import annotations

import pathlib

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.candidates.models import Application, Candidate
from apps.jobs.models import JobOffer
from apps.outreach import backends, registry, services
from apps.outreach.models import Channel, Message

# Les trois moments qui comptent dans un recrutement, dans l'ordre ou un
# candidat les vit.
SCENARIOS = [
    ("invitation_entretien", "Invitation a un entretien", {}),
    (
        "proposition",
        "Reponse positive",
        {
            "conditions": (
                "Le poste est a pourvoir a Casablanca, en contrat a duree "
                "indeterminee, avec une prise de fonction souhaitee au debut "
                "du mois prochain."
            )
        },
    ),
    (
        "refus",
        "Reponse negative",
        {
            "motif": (
                "nous avons retenu un profil disposant d'une experience plus "
                "longue sur la partie infrastructure du poste."
            )
        },
    ),
]


class Command(BaseCommand):
    help = "Fabrique les messages types et les envoie, ou les ecrit en .eml."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Adresse destinataire.")
        parser.add_argument(
            "--dossier", default="courriers",
            help="Ou deposer les .eml quand aucun SMTP n'est configure.",
        )
        parser.add_argument(
            "--fichiers", action="store_true",
            help="Ecrire les .eml meme si un SMTP est configure.",
        )

    def handle(self, *args, **options):
        destinataire = options["to"].strip()
        if "@" not in destinataire:
            raise CommandError(f"Adresse invalide : {destinataire}")

        if getattr(settings, "DEMO_MODE", False):
            raise CommandError(
                "DEMO_MODE est actif : tous les canaux sont fermes. C'est "
                "voulu — une demonstration publique n'expedie pas de courrier."
            )

        smtp = bool(getattr(settings, "EMAIL_HOST", ""))
        vraiment = smtp and not options["fichiers"]

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Etat de l'expedition =="))
        self.stdout.write(f"  destinataire  {destinataire}")
        self.stdout.write(f"  backend       {settings.EMAIL_BACKEND}")
        self.stdout.write(f"  expediteur    {settings.DEFAULT_FROM_EMAIL}")
        if not smtp:
            self.stdout.write(
                self.style.WARNING(
                    "  EMAIL_HOST n'est pas renseigne : rien ne peut partir. "
                    "Les messages seront ecrits en .eml."
                )
            )

        candidature = self._candidature(destinataire)
        try:
            ecrits = self._produire(candidature, destinataire, options, vraiment)
        finally:
            # L'adresse fournie est reelle. Elle ne reste pas dans le jeu de
            # demonstration une fois le controle passe — ni le candidat
            # d'essai, ni les brouillons, que la cascade emporte.
            candidature.candidate.delete()
            self.stdout.write("\n  candidature d'essai supprimee de la base")

        self._epilogue(vraiment, ecrits, destinataire)

    def _produire(self, candidature, destinataire, options, vraiment) -> list:
        dossier = pathlib.Path(options["dossier"])
        if not vraiment:
            dossier.mkdir(parents=True, exist_ok=True)

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Messages =="))
        ecrits = []
        for modele_id, libelle, valeurs in SCENARIOS:
            modele = registry.get(modele_id)
            message = services.rediger(
                candidature,
                modele_id=modele_id,
                channel=Channel.EMAIL,
                actor=None,
                # Le modele de langage n'est pas sollicite : on eprouve ici la
                # chaine d'envoi, pas la redaction. Un serveur d'inference
                # injoignable ne doit pas faire echouer ce controle.
                avec_modele=False,
                **valeurs,
            )
            self.stdout.write(
                f"\n  {libelle} ({modele.id} v{modele.version})"
                f"\n    objet   {message.subject}"
                f"\n    corps   {len(message.body)} caracteres"
            )

            if vraiment:
                services.envoyer(message, actor=None)
                self.stdout.write(self.style.SUCCESS("    envoye"))
                continue

            chemin = dossier / f"{modele_id}.eml"
            chemin.write_bytes(self._eml(message, destinataire))
            ecrits.append(chemin)
            self.stdout.write(f"    ecrit   {chemin}")
        return ecrits

    # --- Fabrication ---------------------------------------------------------
    def _candidature(self, destinataire: str) -> Application:
        """Une candidature d'essai, portant l'adresse demandee.

        Creee puis supprimee : le jeu de demonstration ne doit pas garder une
        adresse reelle apres un test.
        """
        offre = (
            JobOffer.objects.filter(status=JobOffer.Status.OPEN).first()
            or JobOffer.objects.first()
        )
        if offre is None:
            raise CommandError(
                "Aucune offre en base. Lancer `python manage.py seed_demo`."
            )
        candidat = Candidate.objects.create(
            full_name="Badr Sahraoui",
            email=destinataire,
            headline="Developpeur backend et IA appliquee",
            total_experience_years=2.0,
        )
        return Application.objects.create(candidate=candidat, offer=offre)

    def _eml(self, message: Message, destinataire: str) -> bytes:
        """Le message MIME complet, tel qu'un client de messagerie le lira."""
        expediteur = backends.EXPEDITEURS[Channel.EMAIL]
        from django.core.mail import EmailMultiAlternatives

        courrier = EmailMultiAlternatives(
            subject=message.subject,
            body=message.body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[destinataire],
        )
        courrier.attach_alternative(backends.habiller(message.body), "text/html")
        courrier.mixed_subtype = "related"
        courrier.attach(backends._marque_liee())
        assert expediteur  # le canal doit exister, sinon le test ne prouve rien
        return courrier.message().as_bytes()

    def _epilogue(self, vraiment: bool, ecrits: list, destinataire: str) -> None:
        if vraiment:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\n{len(SCENARIOS)} message(s) expedie(s) a {destinataire}."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"\n{len(ecrits)} fichier(s) .eml ecrit(s).")
        )
        self.stdout.write(
            "\nOuvrir un .eml par double-clic affiche le message exactement "
            "comme il serait recu, marque comprise. Pour un envoi reel, "
            "renseigner dans le .env :\n"
            "\n  EMAIL_HOST=smtp.gmail.com"
            "\n  EMAIL_PORT=587"
            "\n  EMAIL_HOST_USER=votre.adresse@gmail.com"
            "\n  EMAIL_HOST_PASSWORD=<mot de passe d'application, pas le mot "
            "de passe du compte>"
            "\n  DEFAULT_FROM_EMAIL=Recrutement.IA <votre.adresse@gmail.com>"
            "\n\npuis relancer la meme commande."
        )
