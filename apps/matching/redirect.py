"""Redirection positive : l'offre ou ce candidat passerait.

Un candidat qui n'atteint pas le seuil sur l'offre a laquelle il a postule
disparait. C'est le comportement de tous les ATS, et c'est une perte seche pour
les deux parties : le candidat ne saura jamais qu'une autre offre lui allait,
et l'entreprise laisse partir quelqu'un qu'elle cherchait ailleurs.

Le projet repete que « l'outil classe, il n'ecarte personne ». Ce module en est
la consequence concrete : quand un dossier passe sous le seuil, on regarde les
autres offres ouvertes avant de le laisser tomber.

**C'est un signalement, pas un transfert.** Aucune candidature n'est creee
automatiquement : postuler ailleurs est une decision qui appartient au candidat,
et proposer son dossier a une autre equipe sans le lui demander poserait un
probleme de finalite au sens du RGPD. Le recruteur voit la suggestion, et c'est
tout.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.candidates.models import Application, Candidate
from apps.jobs.models import JobOffer

from . import engine

# Au-dela, la liste cesse d'etre une suggestion et devient un catalogue.
MAX_SUGGESTIONS = 5


@dataclass
class Suggestion:
    """Une offre ou ce candidat ferait mieux que la ou il est."""

    offer: JobOffer
    score: float
    threshold: float
    gain: float
    already_applied: bool = False
    matched_skills: list[str] = field(default_factory=list)

    @property
    def percentage(self) -> int:
        return round(self.score * 100)

    @property
    def gain_points(self) -> float:
        return round(self.gain * 100, 1)

    def as_dict(self) -> dict:
        return {
            "offer": self.offer.slug,
            "title": self.offer.title,
            "score": round(self.score, 4),
            "percentage": self.percentage,
            "gain_points": self.gain_points,
            "already_applied": self.already_applied,
            "matched_skills": self.matched_skills,
        }


@dataclass
class Redirection:
    application: Application
    current_score: float
    threshold: float
    suggestions: list[Suggestion] = field(default_factory=list)
    offers_examined: int = 0

    @property
    def below_threshold(self) -> bool:
        return self.current_score < self.threshold

    @property
    def has_suggestions(self) -> bool:
        return bool(self.suggestions)

    @property
    def current_percentage(self) -> int:
        return round(self.current_score * 100)

    def as_dict(self) -> dict:
        return {
            "application": str(self.application.pk),
            "current_score": round(self.current_score, 4),
            "threshold": round(self.threshold, 4),
            "below_threshold": self.below_threshold,
            "offers_examined": self.offers_examined,
            "suggestions": [item.as_dict() for item in self.suggestions],
        }


def _skills_couvertes(resultat) -> list[str]:
    """Competences obligatoires que ce profil couvre reellement sur l'offre."""
    return [
        match.required
        for match in resultat.skill_matches
        if match.requirement == "required" and match.score >= 0.5
    ]


def for_application(
    application: Application, *, threshold: float | None = None
) -> Redirection:
    """Cherche les offres ouvertes ou ce candidat depasserait le seuil.

    Le score courant est relu depuis le dernier calcul enregistre plutot que
    recalcule : c'est celui que le recruteur a sous les yeux, et en recalculer
    un autre ferait diverger la page d'avec elle-meme.
    """
    from apps.evaluation import threshold as calibration

    seuil = threshold if threshold is not None else calibration.recommended_threshold()

    dernier = application.scores.order_by("-created_at").first()
    courant = dernier.effective_score if dernier else 0.0

    resultat = Redirection(
        application=application, current_score=courant, threshold=seuil
    )
    if not resultat.below_threshold:
        # Le candidat passe la ou il est : il n'y a rien a rattraper.
        return resultat

    candidat = Candidate.objects.prefetch_related(
        "skills", "languages", "certifications"
    ).get(pk=application.candidate_id)

    deja_postulees = set(
        Application.objects.filter(candidate=candidat).values_list("offer_id", flat=True)
    )

    autres = (
        JobOffer.objects.filter(status=JobOffer.Status.OPEN)
        .exclude(pk=application.offer_id)
        .prefetch_related("skills", "languages")
    )

    for offre in autres:
        resultat.offers_examined += 1
        score = engine.score(candidat, offre)
        if score.overall < seuil:
            continue
        resultat.suggestions.append(
            Suggestion(
                offer=offre,
                score=score.overall,
                threshold=seuil,
                gain=score.overall - courant,
                already_applied=offre.pk in deja_postulees,
                matched_skills=_skills_couvertes(score),
            )
        )

    resultat.suggestions.sort(key=lambda item: item.score, reverse=True)
    resultat.suggestions = resultat.suggestions[:MAX_SUGGESTIONS]
    return resultat


def for_offer(offer: JobOffer, *, threshold: float | None = None) -> list[Redirection]:
    """Toutes les redirections possibles pour les candidatures d'une offre.

    Le recruteur qui vient de trier une offre voit d'un coup qui, parmi ceux
    qu'il n'a pas retenus, irait ailleurs.
    """
    candidatures = (
        offer.applications.select_related("candidate", "offer")
        .exclude(stage__in=[Application.Stage.WITHDRAWN, Application.Stage.HIRED])
        .prefetch_related("candidate__skills", "scores")
    )
    redirections = [
        for_application(candidature, threshold=threshold) for candidature in candidatures
    ]
    return [item for item in redirections if item.has_suggestions]
