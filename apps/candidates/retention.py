"""Purge des dossiers de candidature arrives a echeance.

Le RGPD n'autorise pas a conserver indefiniment les donnees d'un candidat qui
n'a pas ete recrute. Le modele portait une date de fin de conservation depuis
l'origine ; rien ne l'ecrivait et rien ne la faisait respecter. C'est fait ici.

La suppression est definitive et en cascade : CV, profil extrait, preuves,
scores, questions d'entretien. Seul le journal d'audit survit — il ne contient
que des identifiants et des compteurs, jamais de donnee personnelle, et c'est
precisement ce qui permet de prouver que la purge a eu lieu.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from django.db import transaction

from apps.core.models import AuditLog
from apps.core.services import record_audit

from .models import Candidate

logger = logging.getLogger(__name__)

# Fenetre pendant laquelle un dossier est signale comme bientot expire.
WARNING_WINDOW_DAYS = 30


@dataclass
class PurgeReport:
    due: int = 0
    deleted: int = 0
    dry_run: bool = False
    names: list[str] = field(default_factory=list)

    @property
    def nothing_to_do(self) -> bool:
        return self.due == 0


def expired(reference: dt.date | None = None):
    """Dossiers dont la date de conservation est depassee."""
    jour = reference or dt.date.today()
    return Candidate.objects.filter(retention_until__lt=jour)


def expiring_soon(days: int = WARNING_WINDOW_DAYS, reference: dt.date | None = None):
    """Dossiers arrivant a echeance dans les `days` jours."""
    jour = reference or dt.date.today()
    return Candidate.objects.filter(
        retention_until__gte=jour, retention_until__lte=jour + dt.timedelta(days=days)
    )


@transaction.atomic
def purge(*, dry_run: bool = False, actor=None, reference: dt.date | None = None) -> PurgeReport:
    """Supprime les dossiers arrives a echeance.

    `dry_run` permet de voir ce qui serait supprime sans rien detruire : une
    purge irreversible merite d'etre regardee avant d'etre lancee.
    """
    a_purger = list(expired(reference))
    rapport = PurgeReport(
        due=len(a_purger),
        dry_run=dry_run,
        names=[candidat.full_name for candidat in a_purger[:20]],
    )

    if dry_run or not a_purger:
        return rapport

    identifiants = [str(candidat.pk) for candidat in a_purger]
    echeances = [str(candidat.retention_until) for candidat in a_purger]
    for candidat in a_purger:
        candidat.delete()
    rapport.deleted = len(identifiants)

    # Le journal garde la trace de la purge sans conserver de donnee
    # personnelle : des identifiants et un compte, rien de nominatif.
    record_audit(
        AuditLog.Action.DATA_PURGED,
        actor=actor,
        summary=f"{rapport.deleted} dossier(s) purge(s) apres expiration",
        candidate_ids=identifiants,
        retention_dates=echeances,
    )
    logger.info("Purge RGPD : %s dossier(s) supprime(s)", rapport.deleted)
    return rapport
