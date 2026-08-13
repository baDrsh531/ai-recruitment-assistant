"""Verifie la configuration courriel, sans rien envoyer et sans rien afficher.

    python manage.py check_email

Repond a une seule question : **le serveur accepte-t-il ces identifiants ?**

Elle existe parce que l'echec le plus courant n'est pas dans le code mais dans
la configuration, et que le diagnostic se noie sinon dans une trace de trente
lignes. Ici chaque cause a son message et sa correction.

Le mot de passe n'est jamais affiche, meme partiellement : quatre caracteres
d'un secret sont quatre caracteres de moins a deviner.
"""

from __future__ import annotations

import smtplib
import sys

from django.conf import settings
from django.core.mail import get_connection
from django.core.management.base import BaseCommand

# Un mot de passe d'application Google fait seize lettres minuscules, sans
# chiffre ni symbole. Un secret qui n'y ressemble pas est presque toujours le
# mot de passe du compte, que Google refuse depuis 2022.
LONGUEUR_APP_PASSWORD = 16

# Le serveur a reconnu les identifiants et refuse quand meme le compte :
# activation en attente, suspension, ou envoi non encore ouvert. Distinct de
# 535, qui designe des identifiants faux.
CODE_COMPTE_NON_AUTORISE = 525


class Command(BaseCommand):
    help = "Teste la connexion SMTP. N'envoie aucun message."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("\n== Configuration =="))

        hote = getattr(settings, "EMAIL_HOST", "")
        utilisateur = getattr(settings, "EMAIL_HOST_USER", "")
        secret = getattr(settings, "EMAIL_HOST_PASSWORD", "")
        expediteur = getattr(settings, "DEFAULT_FROM_EMAIL", "")

        self.stdout.write(f"  backend       {settings.EMAIL_BACKEND}")
        self.stdout.write(f"  hote          {hote or '(vide)'}")
        self.stdout.write(f"  port          {getattr(settings, 'EMAIL_PORT', '-')}")
        self.stdout.write(f"  utilisateur   {utilisateur or '(vide)'}")
        self.stdout.write(
            f"  mot de passe  {'renseigne' if secret else '(vide)'}"
        )
        self.stdout.write(f"  expediteur    {expediteur}")

        if not hote:
            self.stdout.write(
                self.style.WARNING(
                    "\nEMAIL_HOST est vide : les messages ne partent pas, ils "
                    "s'affichent dans la console. Renseigner EMAIL_HOST dans "
                    "le .env — pas dans .env.example, qui est suivi par git."
                )
            )
            return

        # Sans identifiants, Django n'appelle pas `login()` du tout : la
        # connexion s'ouvre, et l'outil annoncerait « prete » alors que rien
        # n'a ete verifie. Un controle qui reussit a vide est pire qu'absent.
        if not utilisateur or not secret:
            self._echec(
                "Identifiants incomplets : rien n'a ete verifie.",
                "EMAIL_HOST_USER et EMAIL_HOST_PASSWORD doivent etre "
                "renseignes dans le .env. Sans eux la connexion s'ouvre sans "
                "authentification, et l'envoi sera refuse au moment ou il "
                "compte.",
                "utilisateur vide" if not utilisateur else "mot de passe vide",
            )

        self._avertir(hote, utilisateur, secret, expediteur)

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Connexion =="))
        connexion = get_connection(fail_silently=False)
        try:
            connexion.open()
        except smtplib.SMTPAuthenticationError as exc:
            # 525 n'est pas un refus d'identifiants : le serveur les a reconnus
            # et refuse le compte. Confondre les deux envoie chercher le
            # probleme du mauvais cote — on regenere une cle qui etait bonne.
            if exc.smtp_code == CODE_COMPTE_NON_AUTORISE:
                self._echec(
                    "Identifiants reconnus, mais le compte n'a pas le droit "
                    "d'envoyer.",
                    "Ce n'est pas la cle : la regenerer ne changera rien. Le "
                    "fournisseur n'a pas encore ouvert l'envoi sur ce compte, "
                    "ou l'a suspendu. Chez Brevo, un compte neuf doit etre "
                    "valide — repondre au questionnaire d'usage et lire les "
                    "bannieres du tableau de bord sur https://app.brevo.com. "
                    "La validation est parfois manuelle et prend quelques "
                    "heures.",
                    exc.smtp_code,
                )
            self._echec(
                "Le serveur a refuse les identifiants.",
                (
                    "Chez Gmail, le mot de passe du compte ne fonctionne plus "
                    "depuis 2022 : il faut un mot de passe d'application, cree "
                    "sur https://myaccount.google.com/apppasswords, et la "
                    "double authentification doit etre active."
                    if "gmail" in hote.lower()
                    else "Verifier le login et la cle SMTP dans l'espace du "
                    "fournisseur. Chez la plupart d'entre eux, le login n'est "
                    "pas l'adresse du compte mais un identifiant dedie, et la "
                    "cle se regenere sans prevenir si elle a ete revoquee."
                ),
                exc.smtp_code,
            )
        # `OSError` suffit : le nom introuvable et le delai depasse en derivent
        # tous les deux. Les enumerer a cote n'ajoutait rien et signalait le
        # contraire de ce que le code fait.
        except OSError as exc:
            self._echec(
                "Le serveur n'a pas repondu.",
                "Verifier EMAIL_HOST et EMAIL_PORT, et qu'aucun pare-feu ne "
                "bloque le port sortant. Gmail attend 587 avec TLS, ou 465 "
                "avec SSL.",
                exc,
            )
        except smtplib.SMTPException as exc:
            self._echec("Le dialogue SMTP a echoue.", str(exc), exc)
        else:
            connexion.close()
            self.stdout.write(
                self.style.SUCCESS(
                    "\n  Identifiants acceptes. La chaine est prete.\n\n"
                    "  python manage.py outreach_selftest --to vous@example.com"
                )
            )

    def _avertir(self, hote: str, utilisateur: str, secret: str, expediteur: str) -> None:
        """Ce qui se voit sans se connecter — et seulement si c'est pertinent.

        Les regles dependent du fournisseur. Chez Gmail, l'expediteur doit
        appartenir au compte authentifie et le secret a une forme reconnaissable ;
        chez un fournisseur transactionnel, le login n'est pas une adresse et
        l'expediteur se valide de son cote. Appliquer les regles de l'un a
        l'autre ferait crier l'outil a tort, ce qui apprend vite a ne plus le
        lire.
        """
        gmail = "gmail" in hote.lower()

        if gmail and secret and (
            len(secret) != LONGUEUR_APP_PASSWORD or not secret.islower()
            or not secret.isalpha()
        ):
            self.stdout.write(
                self.style.WARNING(
                    "\n  Ce secret ne ressemble pas a un mot de passe "
                    "d'application Google : ceux-ci font seize lettres "
                    "minuscules, sans chiffre ni symbole. Si c'est le mot de "
                    "passe du compte, il sera refuse."
                )
            )

        if gmail and utilisateur and expediteur and utilisateur not in expediteur:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  DEFAULT_FROM_EMAIL annonce une adresse que le compte "
                    f"authentifie ({utilisateur}) ne possede pas. Gmail "
                    f"reecrira l'expediteur. Aligner les deux, ou declarer "
                    f"l'adresse comme alias verifie."
                )
            )

        if not gmail and expediteur:
            self.stdout.write(
                self.style.HTTP_INFO(
                    f"\n  Un fournisseur transactionnel n'expedie qu'au nom "
                    f"d'une adresse qu'il a validee. Verifier que "
                    f"{expediteur} figure bien parmi les expediteurs confirmes "
                    f"du compte — sinon la connexion reussira et l'envoi sera "
                    f"refuse ensuite."
                )
            )

    def _echec(self, quoi: str, comment: str, detail) -> None:
        self.stdout.write(self.style.ERROR(f"\n  {quoi}"))
        self.stdout.write(f"  {comment}")
        self.stdout.write(self.style.HTTP_INFO(f"\n  detail : {detail}"))
        sys.exit(1)
