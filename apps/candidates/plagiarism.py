"""CV distincts au contenu commun.

A ne pas confondre avec la page « Doublons », qui cherche **une meme personne**
sous deux dossiers. Ici les candidats sont differents et le texte se ressemble :
un CV recopie, un modele partage dans une promotion, une agence qui reformate le
meme profil pour deux clients.

Le fichier identique est deja traite ailleurs — `content_hash` est unique, et
redeposer le meme octet ne cree rien. Ce module s'attaque au cas restant, et
plus difficile : deux fichiers differents dont le texte se recouvre.

**Le tout-venant fausse tout.** « Experience professionnelle », « Langues :
francais, anglais », « Permis B » se retrouvent dans un CV sur deux. Une mesure
naive rapprocherait tout le monde de tout le monde. Deux garde-fous : des
empreintes assez longues pour qu'une formule banale n'en produise pas, et le
retrait de celles qui reviennent dans une large part du corpus — l'equivalent,
au niveau de la phrase, de ce qu'un mot vide est au niveau du mot.

**Ce module n'accuse personne.** Un fort recouvrement peut venir d'une fraude
comme d'un modele d'ecole partage entre camarades de promotion. Il signale des
paires a regarder, et un humain tranche — comme partout ailleurs dans ce projet.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

# Longueur d'une empreinte, en mots. Cinq mots laissent encore passer « nous
# avons mis en place » ; huit ne se retrouvent identiques que si deux textes
# partagent une phrase entiere, ce qui est deja un fait, pas un hasard.
TAILLE_EMPREINTE = 8

# Une empreinte presente dans plus de cette part des documents est du
# tout-venant : elle ne distingue plus rien et se retire.
PART_TROP_COMMUNE = 0.30

# En deca, on ne signale pas. Valeur volontairement haute : un faux positif ici
# porte une accusation, un faux negatif ne fait rien perdre.
SEUIL_SIGNALEMENT = 0.35

# Sous ce nombre d'empreintes, le texte est trop court pour que la mesure
# signifie quoi que ce soit — un CV d'une page a l'extraction ratee, par exemple.
EMPREINTES_MINIMUM = 20

_NON_MOT = re.compile(r"[^\w\s]", re.UNICODE)
_ESPACES = re.compile(r"\s+")


def normaliser(texte: str) -> list[str]:
    """Mots comparables : sans accent, sans ponctuation, sans casse.

    Un CV recopie puis passe au correcteur ne se trahit plus par sa casse ni sa
    ponctuation. Comparer le texte brut manquerait la moitie des cas.
    """
    sans_accent = "".join(
        caractere
        for caractere in unicodedata.normalize("NFKD", texte.lower())
        if not unicodedata.combining(caractere)
    )
    return _ESPACES.sub(" ", _NON_MOT.sub(" ", sans_accent)).split()


def empreintes(texte: str, taille: int = TAILLE_EMPREINTE) -> set[str]:
    """Ensemble des suites de `taille` mots consecutifs."""
    mots = normaliser(texte)
    if len(mots) < taille:
        return set()
    return {
        " ".join(mots[debut : debut + taille])
        for debut in range(len(mots) - taille + 1)
    }


def jaccard(gauche: set[str], droite: set[str]) -> float:
    """Part commune des deux ensembles. 0 = rien, 1 = identiques."""
    if not gauche or not droite:
        return 0.0
    commun = len(gauche & droite)
    return commun / (len(gauche) + len(droite) - commun)


@dataclass(frozen=True)
class Paire:
    """Deux documents dont le texte se recouvre."""

    document_a: object
    document_b: object
    similarite: float
    empreintes_communes: int
    extrait: str

    @property
    def pourcentage(self) -> int:
        return round(self.similarite * 100)

    @property
    def candidats(self) -> tuple[str, str]:
        return (
            getattr(self.document_a.candidate, "full_name", "sans candidat"),
            getattr(self.document_b.candidate, "full_name", "sans candidat"),
        )

    @property
    def gravite(self) -> str:
        """Trois paliers, parce qu'ils appellent trois lectures.

        Au-dela de 80 %, deux textes ne se ressemblent plus : c'est le meme.
        Entre 55 et 80 %, un fond commun important — modele partage ou copie
        remaniee. En dessous, un recouvrement qui merite un coup d'oeil et rien
        de plus.
        """
        if self.similarite >= 0.80:
            return "quasi identique"
        if self.similarite >= 0.55:
            return "fond commun important"
        return "recouvrement"


@dataclass
class Rapport:
    paires: list[Paire]
    documents_compares: int
    documents_ignores: int
    empreintes_retirees: int

    @property
    def lecture(self) -> str:
        if self.documents_compares < 2:
            return (
                "Moins de deux CV exploitables : il n'y a rien a comparer. Le "
                "texte extrait doit etre assez long pour que la mesure "
                "signifie quelque chose."
            )
        if not self.paires:
            return (
                f"{self.documents_compares} CV compares, aucune paire au-dessus "
                f"de {round(SEUIL_SIGNALEMENT * 100)} % de recouvrement."
            )
        return (
            f"{len(self.paires)} paire(s) signalee(s) sur "
            f"{self.documents_compares} CV compares. Un fort recouvrement n'est "
            f"pas une preuve : un modele partage dans une promotion en produit "
            f"autant qu'une copie. C'est une liste a regarder, pas un verdict."
        )


def _extrait_commun(gauche: set[str], droite: set[str]) -> str:
    """La plus longue empreinte partagee, pour donner a voir."""
    communes = gauche & droite
    if not communes:
        return ""
    return max(communes, key=len)[:160]


def comparer(documents, *, seuil: float = SEUIL_SIGNALEMENT) -> Rapport:
    """Compare les CV deux a deux et signale ceux qui se recouvrent.

    La comparaison est quadratique. Sous quelques milliers de CV c'est
    instantane ; au-dela il faudrait un pre-filtrage par empreintes minimales
    (MinHash, LSH). La limite est connue et n'est pas franchie ici.
    """
    retenus, ignores = [], 0
    for document in documents:
        jeu = empreintes(document.raw_text or "")
        if len(jeu) < EMPREINTES_MINIMUM:
            ignores += 1
            continue
        retenus.append((document, jeu))

    # Retrait du tout-venant : une empreinte presente dans une large part du
    # corpus ne distingue plus rien. Sans cette etape, deux CV sans rapport se
    # rejoignent sur « experience professionnelle formation langues permis b ».
    compte = Counter()
    for _, jeu in retenus:
        compte.update(jeu)
    plafond = max(2, int(len(retenus) * PART_TROP_COMMUNE))
    banales = {
        empreinte for empreinte, total in compte.items() if total > plafond
    }
    retenus = [(document, jeu - banales) for document, jeu in retenus]

    paires = []
    for index, (document, jeu) in enumerate(retenus):
        for autre, jeu_autre in retenus[index + 1 :]:
            # Deux CV du meme candidat relevent de la page « Doublons », pas
            # d'ici : les melanger noierait le signal utile.
            if (
                document.candidate_id
                and document.candidate_id == autre.candidate_id
            ):
                continue
            similarite = jaccard(jeu, jeu_autre)
            if similarite < seuil:
                continue
            paires.append(
                Paire(
                    document_a=document,
                    document_b=autre,
                    similarite=similarite,
                    empreintes_communes=len(jeu & jeu_autre),
                    extrait=_extrait_commun(jeu, jeu_autre),
                )
            )

    paires.sort(key=lambda item: -item.similarite)
    return Rapport(
        paires=paires,
        documents_compares=len(retenus),
        documents_ignores=ignores,
        empreintes_retirees=len(banales),
    )


def analyser(*, seuil: float = SEUIL_SIGNALEMENT) -> Rapport:
    """Compare tous les CV dont l'extraction a abouti."""
    from .models import CVDocument

    documents = (
        CVDocument.objects.filter(status=CVDocument.Status.DONE)
        .exclude(raw_text="")
        .select_related("candidate")
        .only("id", "raw_text", "candidate", "original_filename", "created_at")
    )
    return comparer(list(documents), seuil=seuil)
