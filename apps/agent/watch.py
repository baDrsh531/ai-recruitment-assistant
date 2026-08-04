"""La veille : l'agent surveille le systeme, pas seulement les dossiers.

`apps/evaluation/monitoring.py` sait deja recalculer les ratios d'impact, les
dater et alerter. Ce qui lui manquait, c'est quelqu'un pour l'appeler. Un
controle qui n'existe que sur une page qu'un responsable doit penser a ouvrir
est un controle qui ne se declenche jamais entre deux audits — precisement la
periode ou une ponderation nouvelle peut reintroduire un signal identitaire.

Trois choses distinguent cette tache du reste de l'agent.

**Elle ne coute aucun token.** Le ratio d'impact se calcule par le moteur
deterministe sur le jeu annote ; aucun modele n'est appele. La veille n'est
donc pas soumise au plafond de tokens, et surtout : elle continue de tourner
quand le budget est epuise. Un garde-fou qui s'arrete en meme temps que ce
qu'il surveille ne garde rien.

**Elle tourne meme quand l'agent est coupe.** `AGENT_ENABLED` protege la
depense, pas la surveillance. Couper l'agent pour economiser et perdre au
passage le controle de biais serait un mauvais echange.

**Elle ne bloque rien**, comme le module qu'elle appelle. Elle constate, date
et signale ; corriger reste une decision humaine.
"""

from __future__ import annotations

import logging

from apps.core.models import AuditLog
from apps.core.services import record_audit
from apps.evaluation import monitoring

from .pipeline import compte_agent

logger = logging.getLogger(__name__)

# Nombre de controles remontes a l'affichage.
HISTORIQUE = 10


def veiller(*, dataset_name: str = "ranking_v1", actor=None) -> monitoring.Controle:
    """Relance le controle de biais et journalise le passage de l'agent.

    Le controle s'enregistre lui-meme sous `bias_monitored` — c'est lui qui
    repond a « depuis quand ? ». L'entree ecrite ici repond a une autre
    question : « qui a regarde, et quand pour la derniere fois ». Un ratio
    stable depuis six mois et un ratio non mesure depuis six mois se
    ressemblent beaucoup dans un tableau ; ils n'ont pas la meme valeur.
    """
    compte = compte_agent()
    controle = monitoring.check(dataset_name=dataset_name, actor=actor or compte)

    record_audit(
        AuditLog.Action.AGENT_WATCHED,
        actor=actor or compte,
        summary=(
            f"Veille : {len(controle.alertes)} alerte(s), "
            f"pire ratio {controle.pire_ratio:.3f}"
        ),
        agent=True,
        dataset=dataset_name,
        alertes=[item.as_dict() for item in controle.alertes],
        pire_ratio=controle.pire_ratio,
        conforme=controle.conforme,
        stable=controle.stable,
    )
    if controle.alertes:
        logger.warning(
            "Veille de l'agent : %s alerte(s), pire ratio %.3f",
            len(controle.alertes),
            controle.pire_ratio,
        )
    return controle


def dernier_controle() -> AuditLog | None:
    """Derniere veille effectuee, ou None si l'agent n'a jamais surveille."""
    return (
        AuditLog.objects.filter(action=AuditLog.Action.AGENT_WATCHED)
        .order_by("-created_at")
        .first()
    )


def alertes_en_cours() -> list[dict]:
    """Alertes du dernier controle, l'ecart legal d'abord."""
    entree = dernier_controle()
    if entree is None:
        return []
    return list(entree.metadata.get("alertes") or [])


def historique(limit: int = HISTORIQUE) -> list[AuditLog]:
    """Les derniers passages de la veille, du plus recent au plus ancien."""
    return list(
        AuditLog.objects.filter(action=AuditLog.Action.AGENT_WATCHED).order_by(
            "-created_at"
        )[:limit]
    )
