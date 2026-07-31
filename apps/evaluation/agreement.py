"""Accord entre recruteurs, et accord avec le score.

Le projet mesure beaucoup ce que fait le moteur et jamais ce que font les
humains qui s'en servent. C'est un angle mort : un outil dont on affirme qu'il
ne decide rien repose entierement sur la qualite des decisions qu'il assiste.

Deux mesures, qui repondent a deux questions differentes.

**Le kappa de Cohen** dit si deux recruteurs qui voient les memes dossiers
prennent les memes decisions. Un accord brut de 80 % ne veut rien dire quand
90 % des candidatures sont ecartees : on tomberait d'accord par hasard. Le
kappa corrige de l'accord attendu au hasard, et c'est pour cela qu'il vaut
mieux qu'un pourcentage.

**L'ecart au score** dit si un recruteur suit le classement ou s'en detache.
Aucune des deux reponses n'est bonne en soi : un recruteur qui suit toujours le
score n'apporte rien qu'un seuil automatique n'apporterait, et un recruteur qui
s'en detache systematiquement rend le score inutile. C'est l'ecart *par rapport
aux autres* qui se lit — pas l'ecart dans l'absolu.

**Ces chiffres ne notent personne.** Un recruteur qui s'ecarte du score peut
avoir raison : il a vu le candidat, le score a vu un PDF. La mesure sert a
ouvrir une conversation, pas a la clore, et le module le dit a l'ecran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from apps.candidates.models import Application
from apps.core.models import AuditLog

# En deca, le kappa n'est pas interpretable : quelques dossiers communs
# suffisent a le faire passer de 0 a 1 par accident.
MIN_DOSSIERS_COMMUNS = 5

# Lecture usuelle du kappa (Landis & Koch). Les bornes sont conventionnelles
# et le module les affiche comme telles, pas comme un verdict.
PALIERS = [
    (0.81, "presque parfait"),
    (0.61, "substantiel"),
    (0.41, "modere"),
    (0.21, "faible"),
    (0.01, "leger"),
    (-1.0, "nul ou pire que le hasard"),
]

# Decision retenue pour la comparaison : un dossier est « retenu » ou
# « ecarte ». Comparer sept etapes de processus deux a deux produirait un kappa
# illisible et sans usage.
ECARTE = {Application.Stage.REJECTED, Application.Stage.WITHDRAWN}


def libelle_kappa(valeur: float) -> str:
    for seuil, mot in PALIERS:
        if valeur >= seuil:
            return mot
    return PALIERS[-1][1]


@dataclass
class Paire:
    """Deux recruteurs, et leur accord sur les dossiers qu'ils ont tous deux juges."""

    a: str
    b: str
    commun: int
    accords: int
    kappa: float

    @property
    def accord_brut(self) -> float:
        return self.accords / self.commun if self.commun else 0.0

    @property
    def interpretable(self) -> bool:
        return self.commun >= MIN_DOSSIERS_COMMUNS

    @property
    def libelle(self) -> str:
        return libelle_kappa(self.kappa)

    def as_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "commun": self.commun,
            "accord_brut": round(self.accord_brut, 4),
            "kappa": round(self.kappa, 4),
            "interpretable": self.interpretable,
        }


@dataclass
class Recruteur:
    """Ce qu'un recruteur decide, et comment cela se place face au score."""

    nom: str
    decisions: int = 0
    retenus: int = 0
    suivis: int = 0
    ecarts: int = 0

    @property
    def taux_de_retenue(self) -> float:
        return self.retenus / self.decisions if self.decisions else 0.0

    @property
    def accord_avec_le_score(self) -> float:
        mesurables = self.suivis + self.ecarts
        return self.suivis / mesurables if mesurables else 0.0

    @property
    def assez_de_decisions(self) -> bool:
        """Assez de decisions pour lire un taux de retenue.

        Distinct de `mesurable` : un taux de retenue se lit des qu'il y a des
        decisions, meme sur des dossiers jamais scores. Confondre les deux
        faisait disparaitre du tableau un recruteur qui decide beaucoup sur un
        vivier non score — alors que c'est precisement celui qu'on veut voir.
        """
        return self.decisions >= MIN_DOSSIERS_COMMUNS

    @property
    def mesurable(self) -> bool:
        """Assez de dossiers scores pour lire un ecart au score."""
        return (self.suivis + self.ecarts) >= MIN_DOSSIERS_COMMUNS

    def as_dict(self) -> dict:
        return {
            "nom": self.nom,
            "decisions": self.decisions,
            "taux_de_retenue": round(self.taux_de_retenue, 4),
            "accord_avec_le_score": round(self.accord_avec_le_score, 4),
            "assez_de_decisions": self.assez_de_decisions,
            "mesurable": self.mesurable,
        }


@dataclass
class Rapport:
    recruteurs: list[Recruteur] = field(default_factory=list)
    paires: list[Paire] = field(default_factory=list)
    seuil: float = 0.0
    dossiers_decides: int = 0

    @property
    def mesurable(self) -> bool:
        """Y a-t-il de quoi mesurer quoi que ce soit ?

        Deux recruteurs qui n'ont aucun dossier en commun ne peuvent pas etre
        compares. Afficher un kappa dans ce cas serait pire que ne rien
        afficher.
        """
        return any(paire.interpretable for paire in self.paires)

    @property
    def ecart_maximal(self) -> float:
        """Ecart entre le recruteur le plus severe et le plus indulgent.

        Repose sur `assez_de_decisions` et non sur `mesurable` : un taux de
        retenue se lit sans qu'aucun dossier ait ete score.
        """
        taux = [
            item.taux_de_retenue
            for item in self.recruteurs
            if item.assez_de_decisions
        ]
        return round(max(taux) - min(taux), 4) if len(taux) >= 2 else 0.0

    def as_dict(self) -> dict:
        return {
            "seuil": self.seuil,
            "dossiers_decides": self.dossiers_decides,
            "mesurable": self.mesurable,
            "ecart_maximal": self.ecart_maximal,
            "recruteurs": [item.as_dict() for item in self.recruteurs],
            "paires": [item.as_dict() for item in self.paires],
        }


def cohen_kappa(a: list[bool], b: list[bool]) -> float:
    """Accord entre deux series de decisions binaires, corrige du hasard.

    Renvoie 1.0 quand les deux series sont identiques *et* qu'aucun accord
    n'etait attendu au hasard ; 0.0 quand l'accord observe vaut exactement
    l'accord attendu ; une valeur negative quand il fait pire.

    Cas limite qui compte : si les deux evaluateurs mettent la meme etiquette
    partout, l'accord attendu vaut 1 et le kappa est indefini (0/0). On rend
    alors 1.0 — ils sont d'accord sur tout — plutot que de lever une exception
    sur le cas le plus banal en recrutement, ou presque tout est ecarte.
    """
    if not a or len(a) != len(b):
        return 0.0

    total = len(a)
    observe = sum(1 for x, y in zip(a, b, strict=True) if x == y) / total

    part_a = sum(a) / total
    part_b = sum(b) / total
    hasard = part_a * part_b + (1 - part_a) * (1 - part_b)

    if hasard >= 1.0:
        return 1.0 if observe >= 1.0 else 0.0
    return (observe - hasard) / (1 - hasard)


def _decisions_par_recruteur() -> dict[str, dict[str, bool]]:
    """{recruteur: {candidature: retenue}}, lu dans le journal d'audit.

    Le journal garde toutes les decisions successives ; on ne retient que la
    derniere de chaque couple recruteur/candidature, sinon un recruteur qui
    hesite pesterait plus lourd qu'un recruteur decide.
    """
    par_recruteur: dict[str, dict[str, bool]] = {}
    entrees = (
        AuditLog.objects.filter(
            action=AuditLog.Action.STAGE_CHANGED, object_type="Application"
        )
        .exclude(actor__isnull=True)
        .select_related("actor")
        .order_by("created_at")
    )
    for entree in entrees:
        etape = entree.metadata.get("stage")
        if not etape:
            continue
        nom = str(entree.actor)
        par_recruteur.setdefault(nom, {})[entree.object_id] = etape not in ECARTE
    return par_recruteur


def analyse(*, threshold: float | None = None) -> Rapport:
    """Mesure l'accord entre recruteurs et leur ecart au score."""
    from . import threshold as calibration

    seuil = threshold if threshold is not None else calibration.recommended_threshold()
    par_recruteur = _decisions_par_recruteur()

    rapport = Rapport(seuil=seuil)
    rapport.dossiers_decides = len(
        {cle for decisions in par_recruteur.values() for cle in decisions}
    )

    # Score retenu par candidature, pour mesurer l'ecart entre la decision et
    # ce que le classement suggerait.
    scores: dict[str, float] = {}
    for candidature in Application.objects.prefetch_related("scores"):
        dernier = candidature.scores.order_by("-created_at").first()
        if dernier is not None:
            scores[str(candidature.pk)] = dernier.effective_score

    for nom, decisions in sorted(par_recruteur.items()):
        profil = Recruteur(nom=nom, decisions=len(decisions))
        for cle, retenue in decisions.items():
            profil.retenus += int(retenue)
            score = scores.get(cle)
            if score is None:
                continue
            suggere = score >= seuil
            if retenue == suggere:
                profil.suivis += 1
            else:
                profil.ecarts += 1
        rapport.recruteurs.append(profil)

    for (nom_a, decisions_a), (nom_b, decisions_b) in combinations(
        sorted(par_recruteur.items()), 2
    ):
        communs = sorted(set(decisions_a) & set(decisions_b))
        if not communs:
            continue
        serie_a = [decisions_a[cle] for cle in communs]
        serie_b = [decisions_b[cle] for cle in communs]
        rapport.paires.append(
            Paire(
                a=nom_a,
                b=nom_b,
                commun=len(communs),
                accords=sum(1 for x, y in zip(serie_a, serie_b, strict=True) if x == y),
                kappa=round(cohen_kappa(serie_a, serie_b), 4),
            )
        )

    rapport.paires.sort(key=lambda paire: paire.kappa)
    return rapport
