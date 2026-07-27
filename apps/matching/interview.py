"""Generation de questions d'entretien ancrees dans le CV.

Une question d'entretien generique ne sert a rien : elle se prepare, elle se
recite, et elle ne dit rien du candidat qu'on a en face. La regle ici est donc
que **chaque question vise une affirmation precise de son CV** — un projet,
une technologie, une responsabilite — et cherche a la verifier.

Le modele recoit le profil deja extrait et les ecarts deja calcules, jamais le
CV brut : il travaille sur des donnees structurees et tracables, comme pour
l'analyse du score.
"""

from __future__ import annotations

import logging

from django.db import transaction

from apps.ai.client import InferenceError, chat_client
from apps.ai.prompts import get as get_prompt
from apps.candidates.models import Application
from apps.core.models import AuditLog
from apps.core.services import record_audit
from apps.jobs.models import JobSkill

from .engine import ScoreResult
from .models import InterviewQuestion

logger = logging.getLogger(__name__)

DEFAULT_COUNT = 6

INTERVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "theme": {
                        "type": "string",
                        "description": "Competence ou sujet vise, en deux ou trois mots.",
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["verification", "exploration", "mise_en_situation"],
                        "description": (
                            "verification : confirmer un acquis annonce. "
                            "exploration : sonder un ecart avec l'offre. "
                            "mise_en_situation : confronter a un cas concret."
                        ),
                    },
                    "cv_claim": {
                        "type": "string",
                        "description": (
                            "L'affirmation du profil que la question vise, "
                            "recopiee telle qu'elle apparait dans les donnees "
                            "fournies. Chaine vide pour une mise en situation."
                        ),
                    },
                    "question": {"type": "string"},
                    "expected_signals": {
                        "type": "string",
                        "description": (
                            "Ce qu'une bonne reponse contient, pour aider le "
                            "recruteur a evaluer. Deux phrases au plus."
                        ),
                    },
                },
            },
        }
    },
}


@transaction.atomic
def generate(
    application: Application,
    result: ScoreResult,
    *,
    count: int = DEFAULT_COUNT,
    actor=None,
) -> list[InterviewQuestion]:
    """Genere un jeu de questions et remplace le precedent.

    Remplace plutot que cumule : deux generations successives ne doivent pas
    laisser douze questions dont on ne sait plus laquelle vient de quel jeu.
    """
    prompt = get_prompt("interview_questions")
    offer = application.offer
    candidate = application.candidate

    messages = prompt.render(
        job_title=offer.title,
        required_skills=_join(
            skill.name
            for skill in offer.skills.all()
            if skill.requirement == JobSkill.Requirement.REQUIRED
        ),
        gaps=_join(gap["skill"] for gap in result.gaps) or "aucun",
        candidate_summary=_profile(candidate),
        count=count,
    )

    try:
        response = chat_client().chat(
            messages,
            schema=INTERVIEW_SCHEMA,
            schema_name="questions",
            temperature=0.3,
            max_tokens=2048,
            purpose="interview_questions",
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            subject=application,
        )
    except InferenceError as exc:
        logger.warning("Questions indisponibles pour %s : %s", application.pk, exc)
        raise

    application.interview_questions.all().delete()

    questions = [
        InterviewQuestion(
            application=application,
            position=index,
            theme=(item.get("theme") or "").strip()[:120],
            intent=_valid_intent(item.get("intent")),
            cv_claim=(item.get("cv_claim") or "").strip()[:400],
            question=(item.get("question") or "").strip(),
            expected_signals=(item.get("expected_signals") or "").strip(),
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            model=response.model,
        )
        for index, item in enumerate(response.parsed.get("questions", []))
        if (item.get("question") or "").strip()
    ]
    InterviewQuestion.objects.bulk_create(questions)

    record_audit(
        AuditLog.Action.SCORE_COMPUTED,
        actor=actor,
        obj=application,
        summary=f"{len(questions)} question(s) d'entretien generees",
        prompt_version=prompt.version,
        model=response.model,
        tokens=response.total_tokens,
    )
    return questions


def _valid_intent(value: object) -> str:
    valid = {choice for choice, _ in InterviewQuestion.Intent.choices}
    candidate = (value or "").strip() if isinstance(value, str) else ""
    return candidate if candidate in valid else InterviewQuestion.Intent.VERIFICATION


def _join(values) -> str:
    listed = list(values)[:12]
    return ", ".join(listed) if listed else ""


def _profile(candidate) -> str:
    """Le modele ne voit que des donnees structurees, jamais le CV brut."""
    lignes = [f"Titre : {candidate.headline or 'non precise'}"]

    competences = list(candidate.skills.all()[:14])
    if competences:
        lignes.append(
            "Competences declarees : "
            + ", ".join(
                f"{skill.name} ({skill.years:.0f} ans)" if skill.years else skill.name
                for skill in competences
            )
        )

    experiences = list(candidate.experiences.all()[:4])
    if experiences:
        lignes.append("Experiences :")
        for experience in experiences:
            description = (experience.description or "").strip()
            lignes.append(
                f"  - {experience.title} chez {experience.company or 'employeur non precise'}"
                f" ({experience.duration_years:.1f} ans)"
                + (f" : {description[:220]}" if description else "")
            )

    formations = list(candidate.education.all()[:3])
    if formations:
        lignes.append(
            "Formation : "
            + ", ".join(
                f"{item.degree}" + (f" ({item.graduation_year})" if item.graduation_year else "")
                for item in formations
            )
        )
    return "\n".join(lignes)
