"""Rapprochement des dossiers appartenant a la meme personne.

Un candidat qui postule deux fois, six mois d'ecart et un CV remanie, cree
aujourd'hui deux dossiers : deux scores, deux historiques, et des statistiques
qui le comptent deux fois. Pire, un recruteur peut ecarter le premier sans
savoir que le second existe.

**Rien n'est fusionne automatiquement.** Deux personnes peuvent porter le meme
nom ; une fusion est irreversible et melerait deux dossiers reels. Le module
propose, un recruteur habilite tranche, et la fusion est journalisee — meme
regle que pour une decision de candidature, et pour la meme raison.

Le rapprochement se fait par blocage plutot que par comparaison de toutes les
paires : les dossiers sont regroupes par cle (adresse, nom normalise,
telephone), et seuls ceux qui partagent une cle sont rapproches. Comparer
n(n-1)/2 paires serait quadratique sans rien apporter — deux dossiers qui ne
partagent aucun de ces trois signaux ne sont jamais proposes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from django.db import transaction

from apps.core.models import AuditLog
from apps.core.services import record_audit

from .models import (
    Application,
    Candidate,
    Certification,
    CVDocument,
    Education,
    Experience,
)

# Poids des signaux. L'adresse electronique est le seul identifiant reellement
# discriminant : deux personnes ne la partagent pas. Le nom, si.
POIDS = {
    "email": 0.70,
    "phone": 0.55,
    "name": 0.30,
    "career": 0.25,
}

# En dessous, le rapprochement n'est pas propose : un simple homonyme ne doit
# pas remonter comme doublon probable.
SEUIL = 0.55

# Particules et titres qui ne distinguent personne.
MOTS_VIDES = {"de", "du", "des", "le", "la", "el", "al", "ben", "bin", "van", "von", "di"}


def _sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFKD", texte)
    return "".join(char for char in decompose if not unicodedata.combining(char))


def cle_nom(nom: str) -> str:
    """Nom reduit a ses composantes signifiantes, ordre indifferent.

    « SAHRAOUI Badr », « Badr Sahraoui » et « badr  sahraoui » donnent la meme
    cle : l'ordre nom/prenom varie d'un CV a l'autre, et la casse encore plus.
    """
    nettoye = _sans_accents(nom.lower())
    nettoye = re.sub(r"[^a-z\s]", " ", nettoye)
    jetons = sorted(
        jeton for jeton in nettoye.split() if len(jeton) > 1 and jeton not in MOTS_VIDES
    )
    return " ".join(jetons)


def cle_telephone(telephone: str) -> str:
    """Neuf derniers chiffres : suffisant pour identifier, insensible au prefixe."""
    chiffres = re.sub(r"\D", "", telephone or "")
    return chiffres[-9:] if len(chiffres) >= 9 else ""


def cle_parcours(candidat: Candidate) -> frozenset[str]:
    """Empreinte du parcours : les employeurs cites, normalises.

    Deux CV remanies par la meme personne changent souvent de formulation mais
    rarement d'employeurs. C'est un signal faible seul, utile en appoint.
    """
    return frozenset(
        _sans_accents(experience.company.strip().lower())
        for experience in candidat.experiences.all()
        if experience.company.strip()
    )


@dataclass
class Paire:
    """Deux dossiers rapproches, avec ce qui les rapproche."""

    a: Candidate
    b: Candidate
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def certain(self) -> bool:
        """Une adresse electronique partagee ne laisse guere de doute."""
        return any(raison.startswith("meme adresse") for raison in self.reasons)


@dataclass
class Groupe:
    """Un ensemble de dossiers qui semblent designer la meme personne."""

    candidates: list[Candidate]
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def size(self) -> int:
        return len(self.candidates)

    @property
    def primary(self) -> Candidate:
        """Dossier a conserver par defaut : le plus ancien, donc le plus complet
        en historique de candidatures."""
        return min(self.candidates, key=lambda candidat: candidat.created_at)

    @property
    def others(self) -> list[Candidate]:
        garde = self.primary
        return [candidat for candidat in self.candidates if candidat.pk != garde.pk]

    @property
    def applications_count(self) -> int:
        return sum(candidat.applications.count() for candidat in self.candidates)


# --- Rapprochement -----------------------------------------------------------
def _comparer(a: Candidate, b: Candidate) -> Paire:
    raisons: list[str] = []
    score = 0.0

    if a.email and a.email.strip().lower() == b.email.strip().lower():
        raisons.append(f"meme adresse : {a.email}")
        score += POIDS["email"]

    telephone = cle_telephone(a.phone)
    if telephone and telephone == cle_telephone(b.phone):
        raisons.append("meme numero de telephone")
        score += POIDS["phone"]

    nom = cle_nom(a.full_name)
    if nom and nom == cle_nom(b.full_name):
        raisons.append(f"meme nom : {a.full_name} / {b.full_name}")
        score += POIDS["name"]

    parcours_a, parcours_b = cle_parcours(a), cle_parcours(b)
    communs = parcours_a & parcours_b
    if communs:
        raisons.append(f"employeur(s) en commun : {', '.join(sorted(communs))}")
        score += POIDS["career"]

    return Paire(a=a, b=b, reasons=raisons, confidence=min(score, 1.0))


def _blocs(candidats: list[Candidate]) -> dict[str, list[Candidate]]:
    blocs: dict[str, list[Candidate]] = {}
    for candidat in candidats:
        cles = []
        if candidat.email.strip():
            cles.append(f"email:{candidat.email.strip().lower()}")
        telephone = cle_telephone(candidat.phone)
        if telephone:
            cles.append(f"tel:{telephone}")
        nom = cle_nom(candidat.full_name)
        if nom:
            cles.append(f"nom:{nom}")
        for cle in cles:
            blocs.setdefault(cle, []).append(candidat)
    return {cle: groupe for cle, groupe in blocs.items() if len(groupe) > 1}


def scan(candidats=None) -> list[Groupe]:
    """Rapproche les dossiers qui semblent designer la meme personne.

    Rien n'est modifie : la fonction ne fait que proposer.
    """
    population = list(
        candidats
        if candidats is not None
        else Candidate.objects.prefetch_related("experiences", "applications")
    )
    if len(population) < 2:
        return []

    par_pk = {candidat.pk: candidat for candidat in population}
    parent = {candidat.pk: candidat.pk for candidat in population}

    def racine(pk):
        while parent[pk] != pk:
            parent[pk] = parent[parent[pk]]
            pk = parent[pk]
        return pk

    paires: list[Paire] = []
    vues: set[tuple] = set()
    for bloc in _blocs(population).values():
        for index, a in enumerate(bloc):
            for b in bloc[index + 1:]:
                cle = tuple(sorted((str(a.pk), str(b.pk))))
                if cle in vues:
                    continue
                vues.add(cle)
                paire = _comparer(a, b)
                if paire.confidence >= SEUIL:
                    paires.append(paire)
                    parent[racine(a.pk)] = racine(b.pk)

    groupes: dict[object, Groupe] = {}
    for paire in paires:
        tete = racine(paire.a.pk)
        groupe = groupes.setdefault(tete, Groupe(candidates=[]))
        for candidat in (paire.a, paire.b):
            if all(membre.pk != candidat.pk for membre in groupe.candidates):
                groupe.candidates.append(par_pk[candidat.pk])
        for raison in paire.reasons:
            if raison not in groupe.reasons:
                groupe.reasons.append(raison)
        groupe.confidence = max(groupe.confidence, paire.confidence)

    resultat = list(groupes.values())
    resultat.sort(key=lambda groupe: (-groupe.confidence, -groupe.size))
    return resultat


# --- Fusion ------------------------------------------------------------------
class MergeRefused(ValueError):
    """La fusion demandee n'est pas recevable."""


@transaction.atomic
def merge(keep: Candidate, others: list[Candidate], *, actor, request=None) -> Candidate:
    """Fusionne des dossiers dans `keep`. Irreversible, donc jamais automatique.

    Les regles de report suivent une seule idee : ne rien perdre. Une
    competence presente des deux cotes garde l'anciennete la plus elevee, une
    candidature en double garde celle qui est allee le plus loin dans le
    processus, et l'echeance de conservation retenue est la plus tardive — la
    candidature la plus recente est ce qui justifie de conserver le dossier.
    """
    if actor is None or not getattr(actor, "can_decide", False):
        raise MergeRefused("Ce compte n'est pas habilite a fusionner des dossiers.")

    autres = [candidat for candidat in others if candidat.pk != keep.pk]
    if not autres:
        raise MergeRefused("Aucun dossier a fusionner.")

    absorbes = [str(candidat.pk) for candidat in autres]
    noms = {candidat.full_name for candidat in autres}

    for candidat in autres:
        _reporter_competences(keep, candidat)
        _reporter_candidatures(keep, candidat)
        _reporter_langues(keep, candidat)
        Experience.objects.filter(candidate=candidat).update(candidate=keep)
        Education.objects.filter(candidate=candidat).update(candidate=keep)
        Certification.objects.filter(candidate=candidat).update(candidate=keep)
        CVDocument.objects.filter(candidate=candidat).update(candidate=keep)

        keep.total_experience_years = max(
            keep.total_experience_years, candidat.total_experience_years
        )
        keep.highest_education = max(keep.highest_education, candidat.highest_education)
        for champ in ["email", "phone", "location", "headline", "linkedin_url", "github_url"]:
            if not getattr(keep, champ) and getattr(candidat, champ):
                setattr(keep, champ, getattr(candidat, champ))
        if candidat.retention_until and (
            keep.retention_until is None or candidat.retention_until > keep.retention_until
        ):
            keep.retention_until = candidat.retention_until

    keep.save()
    for candidat in autres:
        candidat.delete()

    record_audit(
        AuditLog.Action.CANDIDATES_MERGED,
        actor=actor,
        obj=keep,
        summary=f"{len(autres) + 1} dossiers fusionnes en un seul",
        request=request,
        kept=str(keep.pk),
        merged=absorbes,
        merged_names=sorted(noms),
    )
    return keep


def _reporter_competences(keep: Candidate, source: Candidate) -> None:
    existantes = {skill.normalized_name: skill for skill in keep.skills.all()}
    for competence in source.skills.all():
        deja = existantes.get(competence.normalized_name)
        if deja is None:
            competence.candidate = keep
            competence.save(update_fields=["candidate"])
            existantes[competence.normalized_name] = competence
            continue
        # Doublon : on garde l'anciennete et l'usage les plus favorables.
        deja.years = max(deja.years, competence.years)
        deja.last_used_year = max(
            deja.last_used_year or 0, competence.last_used_year or 0
        ) or None
        deja.save(update_fields=["years", "last_used_year"])
        competence.delete()


def _reporter_langues(keep: Candidate, source: Candidate) -> None:
    connues = {item.language.strip().lower() for item in keep.languages.all()}
    for langue in source.languages.all():
        if langue.language.strip().lower() in connues:
            langue.delete()
            continue
        langue.candidate = keep
        langue.save(update_fields=["candidate"])
        connues.add(langue.language.strip().lower())


# Ordre du processus, du plus avance au moins avance. Sert a departager deux
# candidatures du meme candidat sur la meme offre.
AVANCEMENT = {
    Application.Stage.HIRED: 8,
    Application.Stage.OFFER: 7,
    Application.Stage.FINAL: 6,
    Application.Stage.TECHNICAL: 5,
    Application.Stage.PHONE: 4,
    Application.Stage.SCREENING: 3,
    Application.Stage.RECEIVED: 2,
    Application.Stage.REJECTED: 1,
    Application.Stage.WITHDRAWN: 0,
}


def _reporter_candidatures(keep: Candidate, source: Candidate) -> None:
    par_offre = {
        candidature.offer_id: candidature for candidature in keep.applications.all()
    }
    for candidature in source.applications.all():
        existante = par_offre.get(candidature.offer_id)
        if existante is None:
            candidature.candidate = keep
            candidature.save(update_fields=["candidate"])
            par_offre[candidature.offer_id] = candidature
            continue

        avance_source = AVANCEMENT.get(candidature.stage, 0)
        avance_gardee = AVANCEMENT.get(existante.stage, 0)
        if avance_source > avance_gardee:
            # La plus avancee prend la place : perdre un entretien passe parce
            # que le dossier a ete fusionne serait le pire resultat possible.
            existante.delete()
            candidature.candidate = keep
            candidature.save(update_fields=["candidate"])
            par_offre[candidature.offer_id] = candidature
        else:
            candidature.delete()
