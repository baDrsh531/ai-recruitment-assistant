"""Calcul, enregistrement et classement des scores."""

from __future__ import annotations

import logging
import time

from django.db.models import QuerySet
from django.utils import timezone

from apps.candidates.models import Application
from apps.core.models import AuditLog
from apps.core.services import record_audit
from apps.jobs.models import JobOffer

from . import engine, explain
from .models import MatchScore

logger = logging.getLogger(__name__)


def score_application(
    application: Application, *, with_explanation: bool = True, actor=None
) -> MatchScore:
    """Calcule le score d'une candidature et l'enregistre.

    Le calcul deterministe et la redaction de l'analyse sont deux etapes
    distinctes : si le serveur d'inference est indisponible, le score existe
    quand meme, simplement sans commentaire.
    """
    started = time.perf_counter()
    result = engine.score(application.candidate, application.offer)
    compute_ms = int((time.perf_counter() - started) * 1000)

    score = MatchScore(
        application=application,
        overall=result.overall,
        engine_version=result.engine_version,
        weights_used=result.weights_used,
        breakdown=result.breakdown(),
        skill_matches=[match.as_dict() for match in result.skill_matches],
        gaps=result.gaps,
        semantic_used=result.semantic_used,
        blind=result.blind,
        compute_ms=compute_ms,
    )

    if with_explanation:
        analysis = explain.explain(application, result)
        if analysis:
            score.explanation = analysis["explanation"]
            score.explanation_prompt_id = analysis["prompt_id"]
            score.explanation_prompt_version = analysis["prompt_version"]
            score.explanation_model = analysis["model"]

    score.save()

    record_audit(
        AuditLog.Action.SCORE_COMPUTED,
        actor=actor,
        obj=application,
        summary=f"{application.candidate.full_name} — {score.percentage} %",
        overall=result.overall,
        engine_version=result.engine_version,
        weights_used=result.weights_used,
        semantic_used=result.semantic_used,
        blind=result.blind,
        compute_ms=compute_ms,
        explained=bool(score.explanation),
        gaps=[gap["skill"] for gap in result.gaps],
    )
    return score


def score_offer(
    offer: JobOffer, *, with_explanation: bool = True, actor=None
) -> list[MatchScore]:
    """Score toutes les candidatures encore ouvertes d'une offre."""
    applications = (
        offer.applications.select_related("candidate", "offer")
        .exclude(stage__in=[Application.Stage.WITHDRAWN, Application.Stage.REJECTED])
        .prefetch_related(
            "candidate__skills", "candidate__languages",
            "candidate__certifications", "candidate__experiences",
        )
    )
    return [
        score_application(application, with_explanation=with_explanation, actor=actor)
        for application in applications
    ]


def latest_scores(offer: JobOffer) -> list[MatchScore]:
    """Dernier score de chaque candidature de l'offre, classe par ordre decroissant.

    Chaque calcul cree une ligne (historique conserve) : il faut donc retenir
    la plus recente par candidature avant de classer.
    """
    scores: QuerySet[MatchScore] = (
        MatchScore.objects.filter(application__offer=offer)
        .select_related("application", "application__candidate")
        .order_by("application_id", "-created_at")
    )

    latest: dict[str, MatchScore] = {}
    for score in scores:
        latest.setdefault(str(score.application_id), score)

    return sorted(latest.values(), key=lambda item: item.effective_score, reverse=True)


def override_score(
    score: MatchScore, *, value: float, reason: str, actor, request=None
) -> MatchScore:
    """Enregistre une correction humaine du score.

    Le score calcule n'est pas efface : les deux valeurs coexistent, et la
    correction est imputee a un utilisateur identifie.
    """
    previous = score.effective_score
    score.overridden_score = value
    score.overridden_by = actor
    score.override_reason = reason
    score.save(
        update_fields=[
            "overridden_score", "overridden_by", "override_reason", "updated_at",
        ]
    )

    record_audit(
        AuditLog.Action.SCORE_OVERRIDDEN,
        actor=actor,
        obj=score.application,
        summary=f"{previous:.2f} -> {value:.2f}",
        request=request,
        previous=previous,
        new=value,
        reason=reason,
    )
    return score


def decide(
    application: Application, *, stage: str, note: str, actor, request=None
) -> Application:
    """Fait avancer une candidature. Toute decision est humaine et journalisee."""
    application.stage = stage
    application.decided_by = actor
    application.decided_at = timezone.now()
    application.decision_note = note
    application.save(
        update_fields=["stage", "decided_by", "decided_at", "decision_note", "updated_at"]
    )

    record_audit(
        AuditLog.Action.STAGE_CHANGED,
        actor=actor,
        obj=application,
        summary=f"{application.candidate.full_name} -> {application.get_stage_display()}",
        request=request,
        stage=stage,
        note=note,
    )
    return application
