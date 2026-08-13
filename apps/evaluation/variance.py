"""Ce que le modele fait varier, et ce qu'il ne touche jamais.

L'argument central du projet tient en une phrase : **le modele de langage
n'attribue aucune note, il commente un chiffre deja calcule**. Tant qu'elle
reste une phrase, elle vaut ce que vaut une phrase. Ce module la transforme en
mesure.

Le procede est direct : on demande N fois l'analyse du **meme** score, et on
regarde ce qui bouge. La redaction bouge — c'est un modele de langage. Le score,
lui, n'est pas recalcule : il est passe en entree. Un ecart de score entre deux
tirages serait donc impossible par construction, et le verifier revient a
verifier la construction elle-meme.

La mesure qui apprend vraiment quelque chose est ailleurs : **le modele
invente-t-il des chiffres ?** Une analyse qui ecrirait « 72 % sur les
competences » quand le moteur a calcule 68 % serait pire qu'inutile — elle
donnerait au recruteur un chiffre faux avec l'autorite d'un chiffre calcule.
`chiffres_inventes` releve tous les pourcentages du texte et les confronte au
detail du score. C'est deterministe, sans modele, et ca ne coute rien.

Deux tirages sont compares par recouvrement de vocabulaire. **Ce n'est pas une
mesure de sens** : deux textes peuvent dire la meme chose avec d'autres mots, et
la mesure les dira differents. Elle sert a repondre a « le modele repete-t-il sa
copie ou reformule-t-il », pas a juger la qualite.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

# Tout pourcentage ecrit dans le texte, avec ou sans espace avant le signe.
_POURCENTAGE = re.compile(r"(\d{1,3})\s*%")

# Ecart tolere entre un chiffre cite et un chiffre du score. Le modele est prie
# d'arrondir, pas d'inventer : un point d'ecart vient d'un arrondi, dix d'une
# invention.
TOLERANCE_POINTS = 1


@dataclass(frozen=True)
class Tirage:
    """Une analyse redigee, parmi N sur le meme score."""

    texte: str
    modele: str = ""

    @property
    def mots(self) -> list[str]:
        return re.findall(r"\w+", self.texte.lower())

    @property
    def vocabulaire(self) -> set[str]:
        return set(self.mots)

    @property
    def longueur(self) -> int:
        return len(self.mots)

    @property
    def pourcentages_cites(self) -> list[int]:
        return [int(valeur) for valeur in _POURCENTAGE.findall(self.texte)]


@dataclass
class Mesure:
    """Ce que N tirages sur le meme score ont donne."""

    score: float
    tirages: list[Tirage] = field(default_factory=list)
    chiffres_attendus: set[int] = field(default_factory=set)
    indisponible: str = ""

    @property
    def nombre(self) -> int:
        return len(self.tirages)

    @property
    def score_stable(self) -> bool:
        """Le score n'a pas bouge — par construction, et c'est le point.

        Il n'est pas recalcule entre deux tirages : il est passe en entree au
        modele, qui ne peut que le mettre en mots. La propriete est structurelle,
        pas statistique.
        """
        return True

    @property
    def recouvrements(self) -> list[float]:
        """Part de vocabulaire commune entre chaque paire de tirages."""
        parts = []
        for index, tirage in enumerate(self.tirages):
            for autre in self.tirages[index + 1 :]:
                gauche, droite = tirage.vocabulaire, autre.vocabulaire
                if not gauche or not droite:
                    continue
                commun = len(gauche & droite)
                parts.append(commun / (len(gauche) + len(droite) - commun))
        return parts

    @property
    def recouvrement_median(self) -> float | None:
        parts = self.recouvrements
        return round(statistics.median(parts), 3) if parts else None

    @property
    def longueurs(self) -> list[int]:
        return [tirage.longueur for tirage in self.tirages]

    @property
    def ecart_de_longueur(self) -> int | None:
        """Amplitude entre le tirage le plus court et le plus long, en mots."""
        if len(self.longueurs) < 2:
            return None
        return max(self.longueurs) - min(self.longueurs)

    @property
    def chiffres_inventes(self) -> list[tuple[int, int]]:
        """Pourcentages cites par le modele qu'aucun chiffre du score ne justifie.

        Renvoie les couples (tirage, valeur citee). C'est la seule mesure de ce
        module qui puisse reveler une faute grave : un chiffre faux presente
        avec l'autorite d'un chiffre calcule.
        """
        fautes = []
        for index, tirage in enumerate(self.tirages, start=1):
            for cite in tirage.pourcentages_cites:
                if not any(
                    abs(cite - attendu) <= TOLERANCE_POINTS
                    for attendu in self.chiffres_attendus
                ):
                    fautes.append((index, cite))
        return fautes

    @property
    def fidele(self) -> bool:
        return not self.chiffres_inventes

    @property
    def lecture(self) -> str:
        if self.indisponible:
            return self.indisponible
        if self.nombre < 2:
            return (
                "Un seul tirage : il n'y a rien a comparer. La variance se "
                "mesure a partir de deux."
            )

        base = (
            f"{self.nombre} analyses du meme score. Le score n'a pas varie — il "
            f"n'est pas recalcule, il est passe en entree au modele, qui ne peut "
            f"que le mettre en mots."
        )
        variation = (
            f" La redaction, elle, varie : {round((1 - (self.recouvrement_median or 0)) * 100)} % "
            f"du vocabulaire change d'un tirage a l'autre, et la longueur bouge "
            f"de {self.ecart_de_longueur} mots."
        )
        if not self.fidele:
            return (
                base + variation + f" **{len(self.chiffres_inventes)} chiffre(s) "
                f"cite(s) ne correspondent a aucun chiffre du score.** C'est la "
                f"faute que ce controle existe pour attraper."
            )
        return (
            base + variation + " Aucun chiffre cite ne s'ecarte du detail "
            "calcule : le modele reformule, il ne recalcule pas."
        )


def _chiffres_du_score(score) -> set[int]:
    """Tous les pourcentages qu'une analyse a le droit de citer."""
    attendus = {round(score.effective_score * 100), round(score.overall * 100)}
    for critere in score.criteria:
        if critere.get("score") is not None:
            attendus.add(round(critere["score"] * 100))
    for poids in (score.weights_used or {}).values():
        attendus.add(round(poids * 100))
    for rapprochement in score.skill_matches or []:
        if rapprochement.get("score") is not None:
            attendus.add(round(rapprochement["score"] * 100))
    # Les annees d'experience et les effectifs s'ecrivent aussi en clair, mais
    # sans signe %. Seul ce qui porte le signe est confronte.
    return attendus


def mesurer(application, *, tirages: int = 3) -> Mesure:
    """Redige N fois l'analyse du meme score, et compare.

    Chaque appel coute des tokens. La commande qui l'expose le rappelle, et le
    nombre de tirages reste modeste par defaut.
    """
    from apps.ai.client import InferenceError
    from apps.matching import explain
    from apps.matching.engine import score as calculer

    dernier = application.scores.order_by("-created_at").first()
    if dernier is None:
        return Mesure(
            score=0.0,
            indisponible="Ce dossier n'a pas de score : il n'y a rien a expliquer.",
        )

    mesure = Mesure(
        score=dernier.effective_score,
        chiffres_attendus=_chiffres_du_score(dernier),
    )
    resultat = calculer(application.candidate, application.offer, blind=dernier.blind)

    for _ in range(tirages):
        try:
            analyse = explain.explain(application, resultat)
        except InferenceError as exc:
            mesure.indisponible = (
                f"Serveur d'inference injoignable ({exc}). La variance du modele "
                f"ne se mesure pas sans le modele — et le score, lui, reste "
                f"calculable sans lui."
            )
            break
        if not analyse:
            mesure.indisponible = (
                "Le modele n'a rien renvoye. Le score reste affichable : c'est "
                "precisement ce que l'architecture garantit."
            )
            break
        mesure.tirages.append(
            Tirage(texte=analyse["explanation"], modele=analyse.get("model", ""))
        )
    return mesure
