"""Redaction de l'analyse a partir d'un score deja calcule.

Le modele ne voit jamais le CV brut ni l'offre complete : il recoit le detail
chiffre produit par le moteur et le met en mots. Il ne peut donc pas produire
un commentaire en contradiction avec le score affiche, ni reintroduire un
critere que le moteur n'a pas retenu.
"""

from __future__ import annotations

import logging

from apps.ai.client import InferenceError, chat_client
from apps.ai.prompts import get as get_prompt
from apps.candidates.models import Application
from apps.jobs.models import JobSkill

from .engine import ScoreResult

logger = logging.getLogger(__name__)

MAX_SKILLS_LISTED = 12


def explain(application: Application, result: ScoreResult) -> dict:
    """Renvoie {explanation, prompt_id, prompt_version, model} ou {} en cas d'echec."""
    prompt = get_prompt("score_explanation")
    offer = application.offer
    candidate = application.candidate

    messages = prompt.render(
        job_title=offer.title,
        required_skills=_join(
            skill.name for skill in offer.skills.all()
            if skill.requirement == JobSkill.Requirement.REQUIRED
        ),
        preferred_skills=_join(
            skill.name for skill in offer.skills.all()
            if skill.requirement == JobSkill.Requirement.PREFERRED
        ),
        candidate_summary=_candidate_summary(candidate, blind=result.blind),
        score_breakdown=_breakdown_text(result),
    )

    try:
        response = chat_client().chat(
            messages,
            temperature=0.2,
            # L'analyse redigee occupe naturellement 650 a 750 tokens : un
            # plafond a 700 la faisait tronquer un appel sur deux, et le score
            # s'affichait alors sans commentaire, sans explication visible.
            max_tokens=2048,
            purpose="score_explanation",
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            subject=application,
        )
    except InferenceError as exc:
        # Un score sans commentaire reste exploitable ; l'inverse serait faux.
        logger.warning("Analyse indisponible pour %s : %s", application.pk, exc)
        return {}

    return {
        "explanation": response.text,
        "prompt_id": prompt.id,
        "prompt_version": prompt.version,
        "model": response.model,
    }


def _join(names) -> str:
    listed = list(names)[:MAX_SKILLS_LISTED]
    return ", ".join(listed) if listed else "aucune"


def _candidate_summary(candidate, *, blind: bool = False) -> str:
    """Profil transmis au modele. Jamais le nom du candidat, jamais sa ville.

    En mode aveugle, les employeurs sont eux aussi masques : un nom
    d'entreprise renseigne sur le milieu, le reseau et parfois l'origine, sans
    rien dire de la competence.
    """
    lines = [
        f"Titre : {candidate.headline or 'non precise'}",
        f"Experience totale : {candidate.total_experience_years:.1f} ans",
        f"Niveau d'etudes : {candidate.get_highest_education_display()}",
    ]

    skills = list(candidate.skills.all()[:MAX_SKILLS_LISTED])
    if skills:
        lines.append(
            "Competences : "
            + ", ".join(
                f"{skill.name} ({skill.years:.0f} ans)" if skill.years else skill.name
                for skill in skills
            )
        )

    experiences = list(candidate.experiences.all()[:4])
    if experiences:
        lines.append("Postes :")
        for index, experience in enumerate(experiences):
            period = f"{experience.duration_years:.1f} ans"
            employer = (
                f"Entreprise {chr(ord('A') + index)}"
                if blind
                else (experience.company or "employeur non precise")
            )
            lines.append(f"  - {experience.title} chez {employer} ({period})")

    languages = list(candidate.languages.all())
    if languages:
        lines.append(
            "Langues : "
            + ", ".join(f"{item.language} {item.level}" for item in languages)
        )
    return "\n".join(lines)


def _breakdown_text(result: ScoreResult) -> str:
    """Rend le detail chiffre sous une forme lisible par le modele."""
    lines = [f"Score global : {result.overall * 100:.0f} %"]

    for criterion in result.criteria:
        if not criterion.applicable:
            lines.append(
                f"  - {criterion.label} : non applicable "
                f"({criterion.detail.get('reason', 'aucune exigence')})"
            )
            continue
        weight = result.weights_used.get(criterion.name, 0.0)
        lines.append(
            f"  - {criterion.label} : {criterion.score * 100:.0f} % "
            f"(poids {weight * 100:.0f} %)"
        )

    lines.append("\nDetail par competence attendue :")
    for match in result.skill_matches:
        if match.matched_with:
            lines.append(
                f"  - {match.required} : {match.score * 100:.0f} % "
                f"(rapproche de « {match.matched_with} », methode {match.method})"
            )
        else:
            lines.append(f"  - {match.required} : 0 % (aucune correspondance)")

    if result.gaps:
        lines.append(
            "\nEcarts sur des competences obligatoires : "
            + ", ".join(gap["skill"] for gap in result.gaps)
        )

    if not result.semantic_used:
        lines.append(
            "\nNote : le rapprochement semantique etait indisponible, seules "
            "les correspondances exactes et l'ontologie ont ete utilisees."
        )
    return "\n".join(lines)
