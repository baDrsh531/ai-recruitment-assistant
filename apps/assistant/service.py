"""Orchestration de l'assistant : traduire, filtrer, puis rediger.

    question  ─▶  modele : traduction en criteres structures
                   │
                   ▼
              CODE : filtrage sur la base          <- aucun modele ici
                   │
                   ▼
              modele : redaction, a partir des seules lignes trouvees

L'etape du milieu est ce qui distingue cette fonctionnalite d'un simple chat
sur des CV. Le modele ne choisit jamais les candidats : il ne peut donc pas en
inventer, et la meme question renvoie toujours la meme liste.
"""

from __future__ import annotations

import logging
import time

from apps.ai.client import InferenceError, chat_client
from apps.ai.prompts import get as get_prompt
from apps.core.models import AuditLog
from apps.core.services import record_audit
from apps.jobs.models import JobOffer

from .filters import FilterSet, Match, apply
from .models import RecruiterQuery
from .schemas import FILTER_SCHEMA

logger = logging.getLogger(__name__)

MAX_QUESTION = 500


def ask(offer: JobOffer, question: str, *, actor=None, request=None) -> RecruiterQuery:
    """Repond a une question de recruteur sur les candidatures d'une offre."""
    question = (question or "").strip()[:MAX_QUESTION]
    if not question:
        raise ValueError("La question est vide.")

    depart = time.perf_counter()
    client = chat_client()

    filtres, version_filtres = _traduire(client, offer, question)
    resultats = apply(offer, filtres)
    reponse, version_reponse, modele = _rediger(
        client, offer, question, filtres, resultats
    )

    requete = RecruiterQuery.objects.create(
        offer=offer,
        asked_by=actor,
        question=question,
        filters=filtres.as_dict(),
        rejected_criteria=filtres.rejected_criteria,
        matched_ids=[str(item.candidate.pk) for item in resultats],
        matched_count=len(resultats),
        answer=reponse,
        filter_prompt_version=version_filtres,
        answer_prompt_version=version_reponse,
        model=modele,
        latency_ms=int((time.perf_counter() - depart) * 1000),
    )

    record_audit(
        AuditLog.Action.CANDIDATE_VIEWED,
        actor=actor,
        obj=offer,
        summary=f"Question assistant : {question[:120]}",
        request=request,
        filters=filtres.as_dict(),
        matched=len(resultats),
        rejected_criteria=filtres.rejected_criteria,
    )
    return requete


# --- Etapes -----------------------------------------------------------------
def _traduire(client, offer: JobOffer, question: str) -> tuple[FilterSet, str]:
    prompt = get_prompt("search_to_filters")
    reponse = client.chat(
        prompt.render(query=question),
        schema=FILTER_SCHEMA,
        schema_name="filtres",
        max_tokens=1024,
        purpose="search_to_filters",
        prompt_id=prompt.id,
        prompt_version=prompt.version,
        subject=offer,
    )
    return FilterSet.from_payload(reponse.parsed or {}), prompt.version


def _rediger(
    client,
    offer: JobOffer,
    question: str,
    filtres: FilterSet,
    resultats: list[Match],
) -> tuple[str, str, str]:
    prompt = get_prompt("assistant_answer")
    criteres = " ; ".join(filtres.summary()) or "aucun critere explicite"
    ecartes = (
        f"Criteres ecartes car discriminatoires : {', '.join(filtres.rejected_criteria)}\n"
        if filtres.rejected_criteria
        else ""
    )

    try:
        reponse = client.chat(
            prompt.render(
                question=question,
                criteria=criteres,
                rejected=ecartes,
                count=len(resultats),
                candidates=_lignes(resultats) or "aucun",
            ),
            temperature=0.2,
            max_tokens=1500,
            purpose="assistant_answer",
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            subject=offer,
        )
    except InferenceError as exc:
        # La liste reste juste : elle vient du code, pas du modele. Seule la
        # phrase manque, et il vaut mieux le dire que ne rien afficher.
        logger.warning("Redaction indisponible : %s", exc)
        return (
            "La liste ci-dessous est complete et exacte, mais la reponse redigee "
            "n'a pas pu etre produite : le serveur d'inference n'a pas repondu.",
            prompt.version,
            "",
        )

    return reponse.text, prompt.version, reponse.model


def _lignes(resultats: list[Match]) -> str:
    """Les seules donnees que le modele voit : jamais la base entiere."""
    lignes = []
    for index, item in enumerate(resultats, start=1):
        candidat = item.candidate
        competences = ", ".join(skill.name for skill in candidat.skills.all()[:10])
        lignes.append(
            f"{index}. {candidat.full_name} — score {item.percent} % — "
            f"{candidat.total_experience_years:.1f} ans — "
            f"{candidat.headline or 'titre non precise'}\n"
            f"   competences : {competences or 'aucune extraite'}"
        )
    return "\n".join(lignes)
