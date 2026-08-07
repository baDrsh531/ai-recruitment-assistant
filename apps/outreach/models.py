"""Echanges avec les candidats : ce qui a ete envoye, par qui, et si on avait
le droit de le faire.

Deux modeles, et l'ordre entre eux n'est pas anodin.

`Consent` porte l'autorisation de contacter, canal par canal. Il vient avant le
message dans la conception parce qu'il vient avant dans la loi : ecrire a
quelqu'un sur WhatsApp parce qu'on a son numero sur un CV n'est pas la meme
chose que lui repondre par e-mail sur la candidature qu'il vient de deposer.
Un projet qui se reclame de la conformite et qui enverrait des messages sans
modeliser cette difference se contredirait lui-meme.

`Message` porte l'echange. Il existe aussi pour les appels telephoniques et les
messages recus, qui ne sont pas « envoyes » par le systeme : le journal doit
repondre a « qu'a-t-on dit a ce candidat », pas seulement a « qu'a-t-on
expedie ». Un candidat qui exerce son droit d'acces demande le premier.

Un message redige par un modele porte l'identifiant et la version du prompt,
comme l'analyse d'un score. Six mois plus tard, on peut dire quelle instruction
a produit le texte qu'une personne a recu.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.candidates.models import Application, Candidate
from apps.core.models import BaseModel


class Channel(models.TextChoices):
    EMAIL = "email", "E-mail"
    WHATSAPP = "whatsapp", "WhatsApp"
    SMS = "sms", "SMS"
    CALL = "call", "Appel telephonique"
    OTHER = "other", "Autre"


# Canaux qu'un depot de candidature suffit a justifier : le candidat a donne
# cette adresse et ce numero **pour cet usage**, et attend une reponse.
CANAUX_PRESUMES = frozenset({Channel.EMAIL, Channel.CALL})

# Canaux qui demandent un accord explicite. WhatsApp et le SMS arrivent sur un
# telephone personnel, souvent hors des heures de travail, et se lisent comme
# une intrusion la ou un e-mail se lit comme une reponse. Ce decoupage est un
# choix de conception documente, pas un avis juridique : il se change en une
# ligne si le cadre applicable l'exige.
CANAUX_SUR_ACCORD = frozenset({Channel.WHATSAPP, Channel.SMS})


class Consent(BaseModel):
    """Accord — ou refus — de contact sur un canal donne.

    Le dernier enregistrement fait foi. On n'ecrase jamais le precedent :
    prouver qu'un accord existait au moment de l'envoi suppose de conserver
    l'historique, y compris les retraits.
    """

    class Source(models.TextChoices):
        FORM = "form", "Formulaire de candidature"
        VERBAL = "verbal", "Accord verbal, note par un recruteur"
        IMPORTED = "imported", "Repris d'un systeme anterieur"
        WITHDRAWN = "withdrawn", "Retrait demande par le candidat"

    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="consents"
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    granted = models.BooleanField("accorde", default=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.FORM)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="consents_recorded",
    )
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = "consentement"
        verbose_name_plural = "consentements"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["candidate", "channel", "-created_at"])]

    def __str__(self) -> str:
        etat = "accorde" if self.granted else "refuse"
        return f"{self.candidate} · {self.get_channel_display()} : {etat}"


class Message(BaseModel):
    """Un echange avec un candidat, envoye, recu ou seulement consigne."""

    class Direction(models.TextChoices):
        OUTBOUND = "out", "Vers le candidat"
        INBOUND = "in", "Recu du candidat"

    class Status(models.TextChoices):
        DRAFT = "draft", "Brouillon"
        SENT = "sent", "Envoye"
        FAILED = "failed", "Echec d'envoi"
        # Un appel telephonique ou un message recu n'est pas « envoye » par le
        # systeme : il est consigne. Confondre les deux ferait croire a une
        # trace technique la ou il n'y a qu'une declaration humaine.
        LOGGED = "logged", "Consigne"

    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="messages"
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    direction = models.CharField(
        max_length=3, choices=Direction.choices, default=Direction.OUTBOUND
    )
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.DRAFT)

    subject = models.CharField("objet", max_length=255, blank=True)
    body = models.TextField("corps")

    # Modele de message applique, versionne comme les prompts : un modele ne se
    # modifie pas en place, il s'incremente.
    template_id = models.CharField(max_length=40, blank=True)
    template_version = models.CharField(max_length=16, blank=True)

    # Renseignes seulement si un modele de langage a redige le brouillon. Vides
    # signifie que le texte vient du gabarit deterministe ou d'un humain.
    prompt_id = models.CharField(max_length=40, blank=True)
    prompt_version = models.CharField(max_length=16, blank=True)
    model_name = models.CharField(max_length=80, blank=True)

    drafted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="messages_drafted",
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="messages_sent",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "message"
        verbose_name_plural = "messages"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["application", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["channel"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_channel_display()} · {self.get_status_display()}"

    @property
    def redige_par_un_modele(self) -> bool:
        return bool(self.prompt_id)

    @property
    def parti(self) -> bool:
        return self.status == self.Status.SENT

    @property
    def modifiable(self) -> bool:
        """Un message parti ne se reecrit pas : ce serait reecrire l'histoire."""
        return self.status == self.Status.DRAFT

    @property
    def apercu(self) -> str:
        texte = " ".join(self.body.split())
        return texte if len(texte) <= 120 else texte[:117] + "..."
