"""Recherche plein texte sur les profils : BM25, et fusion avec le vectoriel.

L'assistant traduit une question en criteres structures, et c'est ce qu'il faut
quand la question en contient. Mais « qui a travaille sur des systemes de
paiement ? » ne se traduit en aucun filtre : ce n'est ni une competence
declaree, ni une langue, ni un seuil. Il faut alors chercher dans le texte.

**BM25 plutot qu'un LIKE.** Une recherche par sous-chaine classe par hasard :
un profil qui mentionne « paiement » vingt fois et un qui l'evoque une fois
sortent a egalite, et un terme rare comme « SEPA » ne pese pas plus qu'un terme
banal comme « projet ». BM25 corrige les deux : frequence saturante et rarete
du terme dans le corpus.

**Fusion par rang, pas par score.** Quand la couche vectorielle est disponible,
les deux listes sont fusionnees par Reciprocal Rank Fusion. Additionner un
score BM25 (non borne, dependant du corpus) et un cosinus (dans [0, 1])
supposerait une echelle commune qui n'existe pas ; les rangs, eux, se comparent.

**La couche vectorielle est desactivee par defaut**, et ce n'est pas un oubli :
la mesure a montre qu'elle rapprochait « Kubernetes » et « Boulangerie » plus
fortement que des technologies voisines. `EMBEDDING_PROVIDER` la reactive, et
`python manage.py evaluate_search` dit ce que cela change.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from apps.candidates.models import Candidate
from apps.core import arabic

# Parametres BM25 usuels. k1 regle la saturation de la frequence, b l'ampleur
# de la normalisation par la longueur du document.
K1 = 1.5
B = 0.75

# Constante de la fusion par rang. 60 est la valeur de l'article d'origine
# (Cormack et al.) ; elle amortit l'ecart entre les premiers rangs.
RRF_K = 60

# Mots trop frequents pour discriminer, dans les deux langues des CV traites.
MOTS_VIDES = {
    "de", "du", "des", "le", "la", "les", "un", "une", "et", "en", "au", "aux",
    "pour", "par", "sur", "avec", "dans", "sans", "chez", "que", "qui", "quoi",
    "est", "sont", "ete", "etre", "avoir", "plus", "moins", "tres", "ans",
    "the", "and", "for", "with", "from", "was", "were", "been", "has", "have",
    "this", "that", "our", "their", "its", "into", "over", "years", "year",
}

MIN_LONGUEUR = 2


def _sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFKD", texte)
    return "".join(char for char in decompose if not unicodedata.combining(char))


def tokenise(texte: str) -> list[str]:
    """Decoupe en termes comparables.

    Les chiffres sont conserves : « ISO 27001 » perd son sens sans eux, et ce
    sont justement les termes rares qui portent le plus d'information dans un
    CV.

    Un terme compose est emis entier **et** en morceaux. Le garder seulement
    entier ferait echouer « bout en bout » sur un profil ecrivant
    « bout-en-bout » ; ne garder que les morceaux ferait disparaitre « 3-D »,
    dont les deux parties tombent sous la longueur minimale. Emettre les deux
    coute quelques jetons et evite les deux echecs.

    L'arabe est normalise avant decoupage : un PDF stocke des formes de
    presentation, pas les lettres de base. Sans cette etape, chercher « سارة »
    ne trouverait jamais « سارة » — ce sont deux suites de points de code
    differentes.
    """
    texte = arabic.normaliser(texte or "")
    nettoye = _sans_accents(texte.lower())

    jetons: list[str] = []
    # Les mots arabes sont extraits a part : la classe latine ne les capte pas,
    # et ils ne comportent ni casse ni trait d'union a traiter.
    for mot in re.findall(r"[ؠ-يٱ-ۓ]+", nettoye):
        if len(mot) >= MIN_LONGUEUR:
            jetons.append(mot)

    for brut in re.findall(r"[a-z0-9+#.\-]+", nettoye):
        terme = brut.strip(".-")
        if not terme:
            continue
        candidats = [terme]
        if "-" in terme:
            candidats += terme.split("-")
        jetons += [
            item for item in candidats
            if len(item) >= MIN_LONGUEUR and item not in MOTS_VIDES
        ]
    return jetons


def document_de(candidat: Candidate) -> str:
    """Texte representant un profil.

    On assemble le profil extrait plutot que le CV brut : le brut contient les
    en-tetes, les adresses et les mentions legales, qui diluent les termes
    utiles sans jamais repondre a une question de recruteur.
    """
    morceaux = [candidat.full_name, candidat.headline]
    morceaux += [competence.name for competence in candidat.skills.all()]
    for experience in candidat.experiences.all():
        morceaux += [experience.title, experience.company, experience.description]
    for formation in candidat.education.all():
        morceaux += [formation.degree, formation.field_of_study, formation.institution]
    morceaux += [certif.name for certif in candidat.certifications.all()]
    morceaux += [langue.language for langue in candidat.languages.all()]
    return " ".join(morceau for morceau in morceaux if morceau)


@dataclass
class Hit:
    candidate: Candidate
    score: float
    rank: int = 0
    method: str = "bm25"
    matched_terms: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidate_id": str(self.candidate.pk),
            "rank": self.rank,
            "score": round(self.score, 4),
            "method": self.method,
            "matched_terms": self.matched_terms,
        }


class BM25Index:
    """Index inverse en memoire.

    Sous quelques dizaines de milliers de profils, un index reconstruit a la
    volee coute moins qu'un service de recherche a exploiter. Au-dela, c'est
    PostgreSQL full-text ou un moteur dedie qu'il faudrait — la limite est
    connue, elle n'est pas franchie ici.
    """

    def __init__(self, candidats: list[Candidate]) -> None:
        self.candidates = candidats
        self.documents = [tokenise(document_de(candidat)) for candidat in candidats]
        self.frequencies = [Counter(document) for document in self.documents]
        self.lengths = [len(document) for document in self.documents]
        total = sum(self.lengths)
        self.average_length = total / len(self.documents) if self.documents else 0.0

        self.document_frequency: Counter[str] = Counter()
        for frequences in self.frequencies:
            self.document_frequency.update(frequences.keys())

    def __len__(self) -> int:
        return len(self.candidates)

    def _idf(self, terme: str) -> float:
        """Rarete du terme, en variante lissee.

        Le +0.5 des deux cotes evite qu'un terme present dans plus de la moitie
        du corpus recoive un poids negatif — ce qui ferait *baisser* le score
        d'un profil qui le contient.
        """
        total = len(self.documents)
        presents = self.document_frequency.get(terme, 0)
        return math.log(1 + (total - presents + 0.5) / (presents + 0.5))

    def search(self, requete: str, limit: int = 10) -> list[Hit]:
        termes = tokenise(requete)
        if not termes or not self.documents:
            return []

        resultats: list[Hit] = []
        for index, frequences in enumerate(self.frequencies):
            score = 0.0
            trouves: list[str] = []
            longueur = self.lengths[index] or 1
            for terme in termes:
                occurrences = frequences.get(terme, 0)
                if not occurrences:
                    continue
                trouves.append(terme)
                normalisation = K1 * (1 - B + B * longueur / (self.average_length or 1))
                score += self._idf(terme) * occurrences * (K1 + 1) / (
                    occurrences + normalisation
                )
            if score > 0:
                resultats.append(
                    Hit(
                        candidate=self.candidates[index],
                        score=score,
                        matched_terms=sorted(set(trouves)),
                    )
                )

        resultats.sort(key=lambda hit: (-hit.score, hit.candidate.full_name))
        for rang, hit in enumerate(resultats[:limit], start=1):
            hit.rank = rang
        return resultats[:limit]


# --- Couche vectorielle ------------------------------------------------------
def _vector_hits(requete: str, candidats: list[Candidate], limit: int) -> list[Hit]:
    """Classement par similarite d'embeddings, ou liste vide si indisponible."""
    from apps.ai import embeddings

    embedder = embeddings.get_embedder_or_none()
    if embedder is None or not candidats:
        return []

    try:
        import numpy as np

        textes = [document_de(candidat) for candidat in candidats]
        vecteurs = embedder.encode([requete, *textes])
    except Exception:  # noqa: BLE001
        return []

    requete_vecteur, documents = vecteurs[0], vecteurs[1:]
    similarites = documents @ requete_vecteur
    ordre = np.argsort(-similarites)[:limit]

    return [
        Hit(
            candidate=candidats[int(index)],
            score=float(similarites[int(index)]),
            rank=rang,
            method="vectoriel",
        )
        for rang, index in enumerate(ordre, start=1)
    ]


def _fusion(listes: list[list[Hit]], limit: int) -> list[Hit]:
    """Reciprocal Rank Fusion : 1/(k + rang), somme sur les listes.

    On fusionne les rangs et non les scores : un score BM25 n'est pas borne et
    depend du corpus, un cosinus vit dans [0, 1]. Les additionner reviendrait a
    poser une equivalence entre deux echelles sans rapport.
    """
    cumul: dict[str, float] = {}
    reference: dict[str, Hit] = {}
    methodes: dict[str, set[str]] = {}

    for liste in listes:
        for hit in liste:
            cle = str(hit.candidate.pk)
            cumul[cle] = cumul.get(cle, 0.0) + 1 / (RRF_K + hit.rank)
            reference.setdefault(cle, hit)
            methodes.setdefault(cle, set()).add(hit.method)

    ordonnes = sorted(
        cumul.items(), key=lambda item: (-item[1], reference[item[0]].candidate.full_name)
    )
    fusionnes: list[Hit] = []
    for rang, (cle, score) in enumerate(ordonnes[:limit], start=1):
        modele = reference[cle]
        fusionnes.append(
            Hit(
                candidate=modele.candidate,
                score=score,
                rank=rang,
                method="+".join(sorted(methodes[cle])),
                matched_terms=modele.matched_terms,
            )
        )
    return fusionnes


# --- Point d'entree ----------------------------------------------------------
@dataclass
class SearchResult:
    query: str
    hits: list[Hit] = field(default_factory=list)
    corpus_size: int = 0
    semantic_used: bool = False

    @property
    def count(self) -> int:
        return len(self.hits)

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "count": self.count,
            "corpus_size": self.corpus_size,
            "semantic_used": self.semantic_used,
            "results": [hit.as_dict() for hit in self.hits],
        }


def search(requete: str, *, limit: int = 10, hybrid: bool = True) -> SearchResult:
    """Cherche des profils par le texte. Deterministe, sans appel modele."""
    requete = (requete or "").strip()
    if not requete:
        return SearchResult(query="")

    candidats = list(
        Candidate.objects.prefetch_related(
            "skills", "experiences", "education", "certifications", "languages"
        )
    )
    index = BM25Index(candidats)
    lexicaux = index.search(requete, limit=limit * 2 if hybrid else limit)

    resultat = SearchResult(query=requete, corpus_size=len(candidats))
    if not hybrid:
        resultat.hits = lexicaux[:limit]
        return resultat

    vectoriels = _vector_hits(requete, candidats, limit * 2)
    resultat.semantic_used = bool(vectoriels)
    if not vectoriels:
        # Degradation assumee : sans embeddings, l'hybride est le lexical seul,
        # et le dire vaut mieux que laisser croire a une recherche semantique.
        resultat.hits = lexicaux[:limit]
        return resultat

    resultat.hits = _fusion([lexicaux, vectoriels], limit)
    return resultat
