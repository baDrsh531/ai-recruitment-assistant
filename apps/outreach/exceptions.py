"""Refus d'envoi, tous explicites.

Un envoi refuse doit dire **pourquoi** au recruteur, dans des termes qui
appellent une action : « ce candidat n'a pas donne son accord pour WhatsApp »
se corrige, « erreur » ne se corrige pas. Chaque cause a donc son type.
"""

from __future__ import annotations


class EnvoiRefuse(Exception):
    """Base : l'envoi n'a pas eu lieu, et ce n'est pas une panne."""


class ConsentementManquant(EnvoiRefuse):
    """Le candidat n'a pas autorise ce canal."""


class CoordonneeManquante(EnvoiRefuse):
    """On n'a pas d'adresse ou de numero pour ce canal."""


class CanalNonConnecte(EnvoiRefuse):
    """Le canal existe dans le modele de donnees, pas dans l'infrastructure."""


class EnvoiBloque(EnvoiRefuse):
    """La configuration interdit tout envoi reel — demonstration publique."""


class MessageDejaParti(EnvoiRefuse):
    """On ne renvoie pas un message deja envoye."""
