"""Surveillance continue du biais.

L'audit contrefactuel existant est une photographie : il mesure le systeme a un
instant donne, sur le jeu annote. C'est ce qu'il faut pour valider un moteur —
ce n'est pas ce qu'il faut pour l'exploiter. Entre deux audits, une offre peut
etre creee avec une ponderation qui reintroduit un signal identitaire, et
personne ne le saura avant la prochaine campagne de mesure.

Ce module transforme la photographie en releve. A chaque execution, le ratio
d'impact est recalcule et **enregistre** ; l'historique permet de repondre a la
question qui compte pour un exploitant : « depuis quand ? ». Un ecart apparu il
y a six mois n'a pas les memes consequences qu'un ecart apparu hier.

**Il ne bloque rien.** Un systeme qui refuserait de scorer parce qu'un ratio a
baisse mettrait un recruteur devant un ecran vide sans qu'il puisse rien y
faire. Le module constate, date, et alerte ; corriger reste une decision
humaine, comme le reste de ce projet.

Deux niveaux, et la distinction est importante : **l'ecart legal** — un ratio
sous le seuil des quatre cinquiemes — et **la derive**, un ratio qui baisse
sans avoir franchi le seuil. Le second est le signal utile : quand le premier
se declenche, il est deja tard.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from apps.core.models import AuditLog
from apps.core.services import record_audit
from apps.matching.engine import ENGINE_VERSION

from . import bias

# Baisse a partir de laquelle on parle de derive. En deca, c'est du bruit de
# mesure : le jeu annote est petit, et un ratio se deplace par paliers.
SEUIL_DERIVE = 0.05

# Nombre de releves conserves a l'affichage. L'historique complet reste dans le
# journal d'audit, qui est immuable.
HISTORIQUE_AFFICHE = 20


@dataclass
class Releve:
    """Un point de mesure : une dimension, un ratio, une date."""

    dimension: str
    ratio: float
    date: dt.datetime
    engine_version: str = ""

    @property
    def sous_le_seuil(self) -> bool:
        return self.ratio < bias.IMPACT_RATIO_THRESHOLD


@dataclass
class Alerte:
    """Ce qui merite d'etre porte a la connaissance d'un responsable."""

    niveau: str  # derive | ecart_legal
    dimension: str
    ratio: float
    precedent: float | None
    message: str

    @property
    def delta(self) -> float | None:
        if self.precedent is None:
            return None
        return round(self.ratio - self.precedent, 4)

    def as_dict(self) -> dict:
        return {
            "niveau": self.niveau,
            "dimension": self.dimension,
            "ratio": self.ratio,
            "precedent": self.precedent,
            "delta": self.delta,
            "message": self.message,
        }


@dataclass
class Controle:
    """Resultat d'une execution de la surveillance."""

    date: dt.datetime
    releves: list[Releve] = field(default_factory=list)
    alertes: list[Alerte] = field(default_factory=list)
    precedents: dict[str, float] = field(default_factory=dict)
    premier_releve: bool = False

    @property
    def conforme(self) -> bool:
        return not any(item.niveau == "ecart_legal" for item in self.alertes)

    @property
    def stable(self) -> bool:
        return not self.alertes

    @property
    def pire_ratio(self) -> float:
        return min((item.ratio for item in self.releves), default=1.0)

    def as_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "engine_version": ENGINE_VERSION,
            "conforme": self.conforme,
            "stable": self.stable,
            "premier_releve": self.premier_releve,
            "worst_ratio": self.pire_ratio,
            "releves": {item.dimension: item.ratio for item in self.releves},
            "alertes": [item.as_dict() for item in self.alertes],
        }


def derniers_releves() -> dict[str, float]:
    """Dernier ratio connu par dimension, lu dans le journal d'audit.

    Le journal est la source : il est immuable et deja conserve. Ajouter une
    table de mesures ferait un second endroit ou la verite pourrait diverger.
    """
    entree = (
        AuditLog.objects.filter(action=AuditLog.Action.BIAS_MONITORED)
        .order_by("-created_at")
        .first()
    )
    if entree is None:
        return {}
    return {
        nom: float(valeur)
        for nom, valeur in (entree.metadata.get("ratios") or {}).items()
    }


def historique(dimension: str | None = None, limit: int = HISTORIQUE_AFFICHE) -> list[Releve]:
    """Suite des releves, du plus recent au plus ancien."""
    releves: list[Releve] = []
    entrees = AuditLog.objects.filter(
        action=AuditLog.Action.BIAS_MONITORED
    ).order_by("-created_at")[:limit]

    for entree in entrees:
        ratios = entree.metadata.get("ratios") or {}
        for nom, valeur in ratios.items():
            if dimension and nom != dimension:
                continue
            releves.append(
                Releve(
                    dimension=nom,
                    ratio=float(valeur),
                    date=entree.created_at,
                    engine_version=entree.metadata.get("engine_version", ""),
                )
            )
    return releves


def _alerter(dimension: str, ratio: float, precedent: float | None) -> Alerte | None:
    if ratio < bias.IMPACT_RATIO_THRESHOLD:
        return Alerte(
            niveau="ecart_legal",
            dimension=dimension,
            ratio=ratio,
            precedent=precedent,
            message=(
                f"« {dimension} » est a {ratio:.3f}, sous le seuil des quatre "
                f"cinquiemes ({bias.IMPACT_RATIO_THRESHOLD:.2f}). Le screening a "
                f"l'aveugle neutralise ce critere ; l'activer sur les offres "
                f"concernees est la mesure la plus directe."
            ),
        )

    if precedent is not None and precedent - ratio >= SEUIL_DERIVE:
        return Alerte(
            niveau="derive",
            dimension=dimension,
            ratio=ratio,
            precedent=precedent,
            message=(
                f"« {dimension} » passe de {precedent:.3f} a {ratio:.3f}. Le seuil "
                f"legal n'est pas franchi, mais l'ecart se creuse : c'est le "
                f"moment ou corriger coute le moins cher."
            ),
        )
    return None


def check(*, dataset_name: str = "ranking_v1", actor=None, record: bool = True) -> Controle:
    """Recalcule les ratios, les compare au dernier releve, et journalise.

    `record=False` sert a mesurer sans laisser de trace — utile pour un
    affichage, pas pour un controle. Un controle qui ne s'enregistre pas ne
    permet pas de repondre a « depuis quand ? », qui est toute la raison d'etre
    de ce module.
    """
    from django.utils import timezone

    precedents = derniers_releves()
    rapport = bias.audit(dataset_name)

    controle = Controle(
        date=timezone.now(),
        precedents=precedents,
        premier_releve=not precedents,
    )

    for dimension in rapport.dimensions:
        controle.releves.append(
            Releve(
                dimension=dimension.dimension,
                ratio=dimension.impact_ratio,
                date=controle.date,
                engine_version=ENGINE_VERSION,
            )
        )
        alerte = _alerter(
            dimension.dimension,
            dimension.impact_ratio,
            precedents.get(dimension.dimension),
        )
        if alerte is not None:
            controle.alertes.append(alerte)

    # L'ecart legal se lit avant la derive : c'est celui qui engage.
    controle.alertes.sort(key=lambda item: item.niveau != "ecart_legal")

    if record:
        record_audit(
            AuditLog.Action.BIAS_MONITORED,
            actor=actor,
            summary=(
                f"Surveillance du biais : {len(controle.alertes)} alerte(s), "
                f"pire ratio {controle.pire_ratio:.3f}"
            ),
            engine_version=ENGINE_VERSION,
            dataset=dataset_name,
            ratios={item.dimension: item.ratio for item in controle.releves},
            alertes=[item.niveau for item in controle.alertes],
        )
    return controle
