"""Ce qu'il manque a un candidat pour atteindre un seuil.

Un score seul ne dit rien d'actionnable. « 61 % » n'indique ni ou se situe la
faiblesse, ni si l'ecart est comblable. Ce module repond a la question que le
recruteur se pose juste apres : *qu'est-ce qui changerait ce chiffre, et de
combien ?*

La methode est un contrefactuel : on modifie une seule caracteristique du
profil, on rejoue le moteur, on lit la difference. Aucun modele de langage
n'intervient — le moteur est deterministe et coute ~20 ms, on peut donc se
permettre de le rejouer quelques dizaines de fois et de mesurer au lieu
d'estimer.

Deux precautions qui comptent :

* **Le critere de minimalite est le nombre de changements, pas leur cout.**
  Comparer « deux ans d'experience » et « un palier de CECRL » sur une meme
  echelle d'effort supposerait une equivalence que rien ne justifie. On cherche
  donc le plus petit *nombre* de changements atteignant le seuil, et on affiche
  l'effort en clair pour que le recruteur en juge.
* **Ce qui manque au profil n'est pas forcement ce qui manque au candidat.**
  Le profil vient d'une extraction : une competence absente peut etre une
  competence non extraite. Le rapport le dit, sans quoi il ferait passer une
  limite de l'outil pour une lacune de la personne.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from apps.candidates.models import (
    Candidate,
    CandidateLanguage,
    CandidateSkill,
    Certification,
)
from apps.jobs.models import LANGUAGE_LEVEL_ORDER, EducationLevel, JobOffer, JobSkill

from . import engine

# Seuil vise par defaut. Il est deliberement expose : la valeur juste depend de
# la tension du marche sur le poste, et `apps.evaluation.threshold` sait la
# calibrer sur le jeu annote.
DEFAULT_TARGET = 0.75

# Au-dela, le rapport devient une liste de courses illisible. Un ecart qui
# demande plus de six changements n'est pas un ecart, c'est un autre metier.
MAX_STEPS = 6

# Anciennete attribuee a une competence acquise quand l'offre n'en exige aucune.
ASSUMED_YEARS = 1.0


@dataclass(slots=True)
class Lever:
    """Un changement unitaire du profil, et ce qu'il rapporte seul."""

    kind: str  # skill | experience | education | language | certification
    label: str
    action: str
    effort: str
    gain: float
    score_if_applied: float

    @property
    def gain_points(self) -> float:
        """Apport exprime en points de pourcentage, pour l'affichage."""
        return round(self.gain * 100, 1)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "action": self.action,
            "effort": self.effort,
            "gain": round(self.gain, 4),
            "gain_points": self.gain_points,
            "score_if_applied": round(self.score_if_applied, 4),
        }


@dataclass
class Step(Lever):
    """Un levier retenu dans le chemin, avec le score cumule apres application.

    `gain` porte ici l'apport **marginal** : ce que le levier ajoute une fois
    les precedents deja appliques. C'est ce qui rend le tableau lisible — la
    somme des lignes fait bien le total. L'apport du levier pris seul reste
    accessible dans `standalone_gain`, et l'ecart entre les deux est precisement
    la non-additivite due au facteur de recevabilite.

    Ni `slots=True` ni `super()` sans argument ici : `@dataclass(slots=True)`
    reconstruit la classe, et la cellule `__class__` que `super()` utilise
    pointe alors sur l'ancienne. L'appel echoue a l'execution, pas a l'import.
    """

    cumulative_score: float = 0.0
    standalone_gain: float = 0.0

    @property
    def cumulative_percentage(self) -> int:
        return round(self.cumulative_score * 100)

    @property
    def standalone_gain_points(self) -> float:
        return round(self.standalone_gain * 100, 1)

    def as_dict(self) -> dict:
        donnees = Lever.as_dict(self)
        donnees["cumulative_score"] = round(self.cumulative_score, 4)
        donnees["cumulative_percentage"] = self.cumulative_percentage
        donnees["standalone_gain"] = round(self.standalone_gain, 4)
        donnees["standalone_gain_points"] = self.standalone_gain_points
        return donnees


@dataclass(slots=True)
class Report:
    current: float
    target: float
    levers: list[Lever] = field(default_factory=list)
    path: list[Step] = field(default_factory=list)
    reached: bool = False
    ceiling: float = 0.0
    extracted_from_cv: bool = True

    @property
    def already_there(self) -> bool:
        return self.current >= self.target

    @property
    def current_percentage(self) -> int:
        return round(self.current * 100)

    @property
    def target_percentage(self) -> int:
        return round(self.target * 100)

    @property
    def ceiling_percentage(self) -> int:
        return round(self.ceiling * 100)

    def as_dict(self) -> dict:
        return {
            "current": round(self.current, 4),
            "target": round(self.target, 4),
            "reached": self.reached,
            "already_there": self.already_there,
            "ceiling": round(self.ceiling, 4),
            "levers": [lever.as_dict() for lever in self.levers],
            "path": [step.as_dict() for step in self.path],
        }


# --- Profil simule -----------------------------------------------------------
class _Collection:
    """Substitut minimal d'un manager Django, pour un profil non enregistre."""

    def __init__(self, items) -> None:
        self._items = list(items)

    def all(self):
        return list(self._items)


class _Profile:
    """Copie modifiable d'un candidat, jamais ecrite en base.

    Le moteur ne lit que six attributs d'un candidat ; les reproduire coute
    moins cher qu'une transaction annulee, et surtout cela garantit qu'aucune
    simulation ne peut laisser de trace dans un dossier reel.
    """

    def __init__(self, candidate: Candidate) -> None:
        self.total_experience_years = candidate.total_experience_years
        self.highest_education = candidate.highest_education
        self.location = candidate.location
        self.skills = _Collection(candidate.skills.all())
        self.languages = _Collection(candidate.languages.all())
        self.certifications = _Collection(candidate.certifications.all())

    def clone(self) -> _Profile:
        copie = _Profile.__new__(_Profile)
        copie.total_experience_years = self.total_experience_years
        copie.highest_education = self.highest_education
        copie.location = self.location
        copie.skills = _Collection(self.skills.all())
        copie.languages = _Collection(self.languages.all())
        copie.certifications = _Collection(self.certifications.all())
        return copie

    # --- Modifications unitaires ---
    def with_skill(self, name: str, years: float) -> _Profile:
        copie = self.clone()
        normalise = name.strip().lower()
        restantes = [
            skill for skill in copie.skills.all()
            if (skill.normalized_name or skill.name.strip().lower()) != normalise
        ]
        restantes.append(
            CandidateSkill(
                name=name,
                normalized_name=normalise,
                years=years,
                last_used_year=dt.date.today().year,
            )
        )
        copie.skills = _Collection(restantes)
        return copie

    def with_experience(self, years: float) -> _Profile:
        copie = self.clone()
        copie.total_experience_years = years
        return copie

    def with_education(self, level: int) -> _Profile:
        copie = self.clone()
        copie.highest_education = level
        return copie

    def with_language(self, language: str, level: str) -> _Profile:
        copie = self.clone()
        restantes = [
            item for item in copie.languages.all()
            if item.language.strip().lower() != language.strip().lower()
        ]
        restantes.append(CandidateLanguage(language=language, level=level))
        copie.languages = _Collection(restantes)
        return copie

    def with_certification(self, name: str) -> _Profile:
        copie = self.clone()
        copie.certifications = _Collection(
            [*copie.certifications.all(), Certification(name=name)]
        )
        return copie


def _score(profile: _Profile, offer: JobOffer, *, blind: bool) -> float:
    return engine.score(profile, offer, blind=blind).overall


@dataclass(slots=True)
class _Requirements:
    """Exigences de l'offre, lues une seule fois.

    La boucle gloutonne rejoue le moteur des dizaines de fois ; sans ce cache,
    chaque tour relisait les competences de l'offre pour retrouver l'anciennete
    attendue. Mesure sur le pire cas du jeu de demonstration : 112 requetes SQL
    avant, 78 apres. Le reste vient du moteur lui-meme et se traite au chargement
    de l'offre, dans `analyse`.
    """

    skills: dict[str, JobSkill]
    languages: dict[str, object]

    @classmethod
    def of(cls, offer: JobOffer) -> _Requirements:
        return cls(
            skills={skill.name: skill for skill in offer.skills.all()},
            languages={item.language: item for item in offer.languages.all()},
        )


# --- Construction des leviers ------------------------------------------------
def _skill_levers(
    base: _Profile, offer: JobOffer, resultat, exigences: _Requirements, *, blind: bool
) -> list[Lever]:
    leviers: list[Lever] = []
    reference = resultat.overall
    par_nom = {match.required: match for match in resultat.skill_matches}

    for job_skill in exigences.skills.values():
        match = par_nom.get(job_skill.name)
        if match is not None and match.score >= 0.999:
            continue

        annees = float(job_skill.min_years or ASSUMED_YEARS)
        simule = _score(base.with_skill(job_skill.name, annees), offer, blind=blind)
        gain = simule - reference
        if gain <= 0:
            continue

        obligatoire = job_skill.requirement == JobSkill.Requirement.REQUIRED
        if match is not None and match.matched_with and match.score > 0:
            action = (
                f"Porter « {match.matched_with} » au niveau attendu pour "
                f"« {job_skill.name} »"
            )
        else:
            action = f"Acquerir « {job_skill.name} »"

        leviers.append(
            Lever(
                kind="skill",
                label=job_skill.name,
                action=action,
                effort=(
                    f"{annees:g} an(s) de pratique, competence "
                    f"{'obligatoire' if obligatoire else 'souhaitee'}"
                ),
                gain=gain,
                score_if_applied=simule,
            )
        )
    return leviers


def _experience_lever(base: _Profile, offer: JobOffer, resultat, *, blind: bool) -> Lever | None:
    critere = resultat.criterion("experience")
    if critere is None or not critere.applicable or critere.score >= 0.999:
        return None

    manquantes = offer.experience_min_years - base.total_experience_years
    if manquantes <= 0:
        return None

    simule = _score(
        base.with_experience(float(offer.experience_min_years)), offer, blind=blind
    )
    gain = simule - resultat.overall
    if gain <= 0:
        return None

    return Lever(
        kind="experience",
        label="Anciennete",
        action=f"Atteindre {offer.experience_min_years} an(s) d'experience",
        effort=f"{manquantes:.1f} an(s) manquant(s)",
        gain=gain,
        score_if_applied=simule,
    )


def _education_lever(base: _Profile, offer: JobOffer, resultat, *, blind: bool) -> Lever | None:
    critere = resultat.criterion("education")
    if critere is None or not critere.applicable or critere.score >= 0.999:
        return None

    simule = _score(base.with_education(offer.education_level), offer, blind=blind)
    gain = simule - resultat.overall
    if gain <= 0:
        return None

    libelle = dict(EducationLevel.choices).get(offer.education_level, "")
    return Lever(
        kind="education",
        label="Formation",
        action=f"Atteindre le niveau {libelle}",
        effort="diplome a obtenir",
        gain=gain,
        score_if_applied=simule,
    )


def _language_levers(
    base: _Profile, offer: JobOffer, resultat, exigences: _Requirements, *, blind: bool
) -> list[Lever]:
    critere = resultat.criterion("languages")
    if critere is None or not critere.applicable or critere.score >= 0.999:
        return []

    parlees = {
        item.language.strip().lower(): LANGUAGE_LEVEL_ORDER.get(item.level, 0)
        for item in base.languages.all()
    }

    leviers: list[Lever] = []
    for exigence in exigences.languages.values():
        attendu = LANGUAGE_LEVEL_ORDER.get(exigence.min_level, 0)
        actuel = parlees.get(exigence.language.strip().lower(), 0)
        if actuel >= attendu:
            continue

        simule = _score(
            base.with_language(exigence.language, exigence.min_level), offer, blind=blind
        )
        gain = simule - resultat.overall
        if gain <= 0:
            continue

        paliers = attendu - actuel
        leviers.append(
            Lever(
                kind="language",
                label=exigence.language,
                action=f"Atteindre {exigence.get_min_level_display()} en {exigence.language}",
                effort=(
                    f"{paliers} palier(s) CECRL" if actuel
                    else "langue absente du profil"
                ),
                gain=gain,
                score_if_applied=simule,
            )
        )
    return leviers


def _certification_levers(
    base: _Profile, offer: JobOffer, resultat, *, blind: bool
) -> list[Lever]:
    critere = resultat.criterion("certifications")
    if critere is None or not critere.applicable or critere.score >= 0.999:
        return []

    leviers: list[Lever] = []
    for detail in critere.detail.get("certifications", []):
        if detail.get("held"):
            continue
        nom = detail["certification"]
        simule = _score(base.with_certification(nom), offer, blind=blind)
        gain = simule - resultat.overall
        if gain <= 0:
            continue
        leviers.append(
            Lever(
                kind="certification",
                label=nom,
                action=f"Obtenir la certification « {nom} »",
                effort="certification exigee par l'offre",
                gain=gain,
                score_if_applied=simule,
            )
        )
    return leviers


# --- Point d'entree ----------------------------------------------------------
def analyse(
    candidate: Candidate,
    offer: JobOffer,
    *,
    target: float = DEFAULT_TARGET,
    blind: bool | None = None,
) -> Report:
    """Cherche le plus petit nombre de changements portant le score au seuil.

    L'algorithme est glouton : on prend a chaque tour le levier qui rapporte le
    plus *dans l'etat courant*, puis on recalcule. Recalculer est indispensable
    et non decoratif — le facteur de recevabilite est multiplicatif, si bien que
    deux leviers appliques ensemble ne rapportent pas la somme de leurs gains
    individuels. Additionner les gains donnerait un chiffre faux.
    """
    if blind is None:
        blind = offer.blind_screening

    # Le moteur relit `offer.skills` et `offer.languages` a chaque appel, et on
    # l'appelle des dizaines de fois. Recharger l'offre avec ses relations
    # prechargees remplit le cache une bonne fois. Pire cas du jeu de
    # demonstration, les deux caches cumules : 112 requetes et 102 ms -> 7
    # requetes et 14 ms.
    offer = JobOffer.objects.prefetch_related("skills", "languages").get(pk=offer.pk)

    base = _Profile(candidate)
    exigences = _Requirements.of(offer)
    resultat = engine.score(base, offer, blind=blind)
    rapport = Report(
        current=resultat.overall,
        target=target,
        extracted_from_cv=candidate.documents.exists(),
    )

    leviers = [
        *_skill_levers(base, offer, resultat, exigences, blind=blind),
        *_language_levers(base, offer, resultat, exigences, blind=blind),
        *_certification_levers(base, offer, resultat, blind=blind),
    ]
    for unitaire in (
        _experience_lever(base, offer, resultat, blind=blind),
        _education_lever(base, offer, resultat, blind=blind),
    ):
        if unitaire is not None:
            leviers.append(unitaire)

    leviers.sort(key=lambda lever: lever.gain, reverse=True)
    rapport.levers = leviers
    rapport.ceiling = _ceiling(base, offer, leviers, exigences, blind=blind)

    if rapport.already_there:
        rapport.reached = True
        return rapport

    profil, courant, restants = base, resultat.overall, list(leviers)
    while restants and courant < target and len(rapport.path) < MAX_STEPS:
        meilleur, meilleur_profil, meilleur_score = None, None, courant
        for lever in restants:
            candidat_profil = _apply(profil, lever, offer, exigences)
            simule = _score(candidat_profil, offer, blind=blind)
            if simule > meilleur_score:
                meilleur, meilleur_profil, meilleur_score = lever, candidat_profil, simule

        if meilleur is None:
            break

        restants.remove(meilleur)
        marginal = meilleur_score - courant
        profil, courant = meilleur_profil, meilleur_score
        rapport.path.append(
            Step(
                kind=meilleur.kind,
                label=meilleur.label,
                action=meilleur.action,
                effort=meilleur.effort,
                gain=marginal,
                score_if_applied=meilleur.score_if_applied,
                cumulative_score=courant,
                standalone_gain=meilleur.gain,
            )
        )

    rapport.reached = courant >= target
    return rapport


def _apply(
    profile: _Profile, lever: Lever, offer: JobOffer, exigences: _Requirements
) -> _Profile:
    if lever.kind == "skill":
        job_skill = exigences.skills.get(lever.label)
        annees = float((job_skill.min_years if job_skill else 0) or ASSUMED_YEARS)
        return profile.with_skill(lever.label, annees)
    if lever.kind == "experience":
        return profile.with_experience(float(offer.experience_min_years))
    if lever.kind == "education":
        return profile.with_education(offer.education_level)
    if lever.kind == "language":
        exigence = exigences.languages.get(lever.label)
        if exigence is not None:
            return profile.with_language(exigence.language, exigence.min_level)
    if lever.kind == "certification":
        return profile.with_certification(lever.label)
    return profile


def _ceiling(
    base: _Profile,
    offer: JobOffer,
    leviers: list[Lever],
    exigences: _Requirements,
    *,
    blind: bool,
) -> float:
    """Score atteint si *tous* les leviers sont actionnes.

    C'est ce qui permet de dire « inatteignable » plutot que de laisser croire
    qu'il suffirait d'en faire plus : quand le plafond reste sous le seuil,
    l'ecart ne vient pas du candidat mais de la distance entre le profil et
    l'offre.
    """
    profil = base
    for lever in leviers:
        profil = _apply(profil, lever, offer, exigences)
    return _score(profil, offer, blind=blind)
