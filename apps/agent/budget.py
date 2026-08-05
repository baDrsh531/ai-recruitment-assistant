"""Plafond de consommation de l'agent.

Un agent qui enchaine des appels a un serveur d'inference sans limite est une
facture qui court. Et le mode d'echec le plus courant n'est pas la depense
volontaire : c'est la boucle — un dossier qui echoue, qu'on reprend, qui
echoue encore.

Le plafond est donc **dur**. Au dépassement l'agent s'arrete et le dit ; il ne
degrade pas, il ne ralentit pas, il ne previent pas pour continuer quand meme.
Une limite qu'on peut franchir n'est pas une limite.

Le compte se fait sur la journee glissante, lu depuis `AIInvocation` — la
table qui enregistre deja chaque appel modele. Un second compteur divergerait
du premier ; celui-ci est la source.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.ai.models import AIInvocation


@dataclass
class Budget:
    """Ce qui reste a depenser aujourd'hui."""

    limite: int
    consomme: int

    @property
    def illimite(self) -> bool:
        return self.limite <= 0

    @property
    def restant(self) -> int:
        if self.illimite:
            return 2**31
        return max(0, self.limite - self.consomme)

    @property
    def epuise(self) -> bool:
        return not self.illimite and self.consomme >= self.limite

    @property
    def part_consommee(self) -> float:
        if self.illimite:
            return 0.0
        return min(1.0, self.consomme / self.limite)

    def as_dict(self) -> dict:
        return {
            "limite": self.limite,
            "consomme": self.consomme,
            "restant": self.restant,
            "epuise": self.epuise,
            "illimite": self.illimite,
        }


def consommation(depuis: dt.datetime | None = None) -> int:
    """Tokens consommes sur la fenetre, entree et sortie confondues.

    Les deux comptent : un prompt de 4 000 tokens coute meme si la reponse en
    fait 20.
    """
    debut = depuis or (timezone.now() - dt.timedelta(days=1))
    totaux = AIInvocation.objects.filter(created_at__gte=debut).aggregate(
        entree=Sum("prompt_tokens"), sortie=Sum("completion_tokens")
    )
    return (totaux["entree"] or 0) + (totaux["sortie"] or 0)


def actuel() -> Budget:
    """Etat du budget pour la journee glissante."""
    return Budget(
        limite=int(getattr(settings, "AGENT_DAILY_TOKEN_BUDGET", 0) or 0),
        consomme=consommation(),
    )


def agent_actif() -> bool:
    """Interrupteur d'arret.

    Un systeme autonome sans frein accessible ne se deploie pas. Le reglage se
    change sans redeploiement, et l'agent le relit a chaque execution plutot
    qu'au demarrage du processus — sinon l'interrupteur ne servirait qu'apres
    un redemarrage, c'est-a-dire trop tard.
    """
    return bool(getattr(settings, "AGENT_ENABLED", False))
