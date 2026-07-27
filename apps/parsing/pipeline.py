"""Orchestration de l'extraction d'un CV.

Chaine complete :

    fichier -> texte + positions -> diagnostic qualite -> choix de la voie
            -> extraction structuree (JSON Schema contraint)
            -> ancrage des preuves -> persistance -> audit

Le choix de la voie est la piece centrale : un PDF a texte natif propre passe
par le modele texte (rapide, peu couteux) ; un PDF scanne ou multi-colonnes
passe par le modele vision, qui lit les pages comme des images et respecte
l'ordre de lecture des colonnes.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from django.db import transaction
from django.utils import timezone

from apps.ai.client import InferenceError, chat_client, image_part, text_part, vision_client
from apps.ai.prompts import get as get_prompt
from apps.candidates.models import (
    Candidate,
    CandidateLanguage,
    CandidateSkill,
    Certification,
    CVDocument,
    Education,
    EvidenceSpan,
    Experience,
)
from apps.core.models import AuditLog
from apps.core.services import record_audit
from apps.jobs.models import EducationLevel, LanguageLevel

from . import extractors, quality
from .evidence import EvidenceResolver, ResolvedEvidence
from .schemas import CV_SCHEMA

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 24_000
VALID_EDUCATION_LEVELS = [level.value for level in EducationLevel]
VALID_LANGUAGE_LEVELS = {level.value for level in LanguageLevel}


class ParsingError(RuntimeError):
    pass


# --- Point d'entree --------------------------------------------------------
def parse_document(document: CVDocument, *, actor=None) -> Candidate:
    """Extrait un CV de bout en bout et renvoie le candidat cree ou mis a jour."""
    document.status = CVDocument.Status.EXTRACTING
    document.extraction_started_at = timezone.now()
    document.error = ""
    document.save(update_fields=["status", "extraction_started_at", "error", "updated_at"])

    try:
        with document.file.open("rb") as handle:
            data = handle.read()

        extracted = extractors.extract(data, document.original_filename)
        report = quality.assess(extracted)

        document.page_count = extracted.page_count
        document.raw_text = extracted.full_text
        document.quality = report.as_dict()
        document.status = CVDocument.Status.PARSING
        document.save(
            update_fields=["page_count", "raw_text", "quality", "status", "updated_at"]
        )

        payload, method = _structure(document, data, extracted, report)

        resolver = EvidenceResolver(extracted)
        candidate = _persist(document, payload, resolver)

        document.candidate = candidate
        document.method = method
        document.status = CVDocument.Status.DONE
        document.extraction_finished_at = timezone.now()
        document.save(
            update_fields=[
                "candidate", "method", "status", "extraction_finished_at", "updated_at",
            ]
        )

        verified = document.spans.filter(verified=True).count()
        total = document.spans.count()
        record_audit(
            AuditLog.Action.CV_PARSED,
            actor=actor,
            obj=document,
            summary=f"{candidate.full_name or 'candidat sans nom'} via {method}",
            method=method,
            pages=extracted.page_count,
            quality=report.as_dict(),
            evidence_verified=verified,
            evidence_total=total,
            seconds=document.extraction_seconds,
        )
        return candidate

    except Exception as exc:
        document.status = CVDocument.Status.FAILED
        document.error = str(exc)[:2000]
        document.extraction_finished_at = timezone.now()
        document.save(
            update_fields=["status", "error", "extraction_finished_at", "updated_at"]
        )
        logger.exception("Echec d'extraction du document %s", document.pk)
        raise


# --- Choix de la voie et appel modele --------------------------------------
def _structure(
    document: CVDocument,
    data: bytes,
    extracted: extractors.ExtractedDocument,
    report: quality.QualityReport,
) -> tuple[dict, str]:
    """Renvoie (donnees structurees, methode utilisee)."""
    use_vision = report.needs_vision and extracted.source == "pdf"

    if use_vision:
        try:
            payload = _structure_with_vision(document, data, extracted)
            # On garde trace du fait qu'un texte natif existait aussi : les
            # preuves seront ancrees dessus, avec coordonnees a la cle.
            method = (
                CVDocument.Method.HYBRID
                if report.has_text_layer
                else CVDocument.Method.VISION
            )
            return payload, method
        except InferenceError:
            if not report.has_text_layer:
                raise
            logger.warning(
                "Modele vision indisponible pour %s, repli sur le texte natif", document.pk
            )

    if not report.has_text_layer:
        raise ParsingError(
            "Aucun texte exploitable et modele vision indisponible : "
            "le document est probablement un scan."
        )

    return _structure_with_text(document, extracted), CVDocument.Method.TEXT


def _structure_with_text(document: CVDocument, extracted: extractors.ExtractedDocument) -> dict:
    prompt = get_prompt("cv_extraction")
    response = chat_client().chat(
        prompt.render(cv_text=extracted.full_text[:MAX_TEXT_CHARS]),
        schema=CV_SCHEMA,
        schema_name="cv",
        max_tokens=4096,
        purpose="cv_extraction",
        prompt_id=prompt.id,
        prompt_version=prompt.version,
        subject=document,
    )
    return response.parsed or {}


def _structure_with_vision(
    document: CVDocument, data: bytes, extracted: extractors.ExtractedDocument
) -> dict:
    images = extractors.render_pdf_pages(data)
    if not images:
        raise ParsingError("Aucune page n'a pu etre rendue en image.")

    prompt = get_prompt("cv_extraction_vision")
    instruction = prompt.template.format(page_count=len(images))
    messages = [
        {"role": "system", "content": prompt.system},
        {
            "role": "user",
            "content": [text_part(instruction), *(image_part(png) for png in images)],
        },
    ]
    response = vision_client().chat(
        messages,
        schema=CV_SCHEMA,
        schema_name="cv",
        max_tokens=4096,
        purpose="cv_extraction_vision",
        prompt_id=prompt.id,
        prompt_version=prompt.version,
        subject=document,
    )
    return response.parsed or {}


# --- Persistance -----------------------------------------------------------
@transaction.atomic
def _persist(document: CVDocument, payload: dict, resolver: EvidenceResolver) -> Candidate:
    identity = payload.get("identity") or {}
    candidate = _get_or_create_candidate(identity)

    spans: dict[str, EvidenceSpan] = {}

    def span_for(item: dict) -> EvidenceSpan | None:
        quote = (item.get("evidence") or "").strip()
        if not quote:
            return None
        if quote in spans:
            return spans[quote]
        resolved = resolver.resolve(quote)
        span = _create_span(document, quote, resolved)
        spans[quote] = span
        return span

    _persist_skills(candidate, payload.get("skills") or [], span_for)
    _persist_experiences(candidate, payload.get("experiences") or [], span_for)
    _persist_education(candidate, payload.get("education") or [], span_for)
    _persist_languages(candidate, payload.get("languages") or [], span_for)
    _persist_certifications(candidate, payload.get("certifications") or [], span_for)

    candidate.total_experience_years = round(_total_experience_years(candidate), 2)
    levels = [edu.level for edu in candidate.education.all()]
    candidate.highest_education = max(levels) if levels else EducationLevel.NONE
    candidate.save(update_fields=["total_experience_years", "highest_education", "updated_at"])
    return candidate


def _get_or_create_candidate(identity: dict) -> Candidate:
    email = (identity.get("email") or "").strip().lower()
    full_name = (identity.get("full_name") or "").strip()

    fields = {
        "full_name": full_name or "Candidat sans nom",
        "phone": (identity.get("phone") or "").strip()[:40],
        "linkedin_url": _clean_url(identity.get("linkedin")),
        "github_url": _clean_url(identity.get("github")),
        "location": (identity.get("location") or "").strip()[:160],
        "headline": (identity.get("headline") or "").strip()[:200],
    }

    if email:
        candidate, created = Candidate.objects.get_or_create(email=email, defaults=fields)
        if not created:
            for key, value in fields.items():
                if value:
                    setattr(candidate, key, value)
            candidate.save()
        return candidate

    return Candidate.objects.create(**fields)


def _create_span(
    document: CVDocument, quote: str, resolved: ResolvedEvidence | None
) -> EvidenceSpan:
    if resolved is None:
        # Citation introuvable : on la conserve, marquee non verifiee. L'interface
        # signale la donnee comme non etayee plutot que de la faire disparaitre.
        return EvidenceSpan.objects.create(
            document=document, page=1, text=quote[:500], verified=False, match_ratio=0.0
        )
    return EvidenceSpan.objects.create(
        document=document,
        page=resolved.page,
        text=resolved.text[:500],
        bbox=resolved.bbox,
        char_start=resolved.char_start,
        char_end=resolved.char_end,
        verified=resolved.verified,
        match_ratio=resolved.ratio,
    )


def _confidence(span: EvidenceSpan | None) -> float:
    """La confiance suit la qualite de la citation, pas un simple oui/non.

    Une citation retrouvee mot pour mot vaut 1.0 ; une citation breve mais
    exacte vaut 0.75 ; une correspondance partielle vaut son taux ; une
    citation absente du document tombe a 0.3.
    """
    if span is None:
        return 0.4
    if not span.verified:
        return 0.3
    return round(min(1.0, span.match_ratio or 1.0), 2)


def _persist_skills(candidate: Candidate, items: list[dict], span_for) -> None:
    for item in items:
        name = (item.get("name") or "").strip()
        if not name or len(name) > 120:
            continue
        span = span_for(item)
        normalized = name.lower()
        years = max(0.0, float(item.get("years") or 0))
        last_used = item.get("last_used_year") or None
        if last_used and not 1970 <= int(last_used) <= dt.date.today().year + 1:
            last_used = None

        skill, created = CandidateSkill.objects.get_or_create(
            candidate=candidate,
            normalized_name=normalized,
            defaults={
                "name": name,
                "years": years,
                "last_used_year": last_used,
                "evidence": span,
                "confidence": _confidence(span),
            },
        )
        if not created:
            skill.years = max(skill.years, years)
            if last_used:
                skill.last_used_year = max(skill.last_used_year or 0, int(last_used))
            if span and not skill.evidence:
                skill.evidence = span
                skill.confidence = _confidence(span)
            skill.save()


# Les competences et les langues sont dedupliquees par une contrainte d'unicite.
# Les experiences, formations et certifications n'en ont pas : il faut une cle
# naturelle, sans quoi redeposer un CV — ou en deposer un second pour la meme
# personne — empile les doublons. Sept experiences pour un CV qui en compte
# deux, constate en rejouant l'extraction trois fois de suite.
def _persist_experiences(candidate: Candidate, items: list[dict], span_for) -> None:
    for item in items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        span = span_for(item)
        Experience.objects.update_or_create(
            candidate=candidate,
            title=title[:200],
            company=(item.get("company") or "").strip()[:200],
            start_date=_parse_month(item.get("start_date")),
            defaults={
                "location": (item.get("location") or "").strip()[:160],
                "end_date": _parse_month(item.get("end_date")),
                "description": (item.get("description") or "").strip(),
                "evidence": span,
                "confidence": _confidence(span),
            },
        )


def _persist_education(candidate: Candidate, items: list[dict], span_for) -> None:
    for item in items:
        degree = (item.get("degree") or "").strip()
        if not degree:
            continue
        span = span_for(item)
        Education.objects.update_or_create(
            candidate=candidate,
            degree=degree[:200],
            institution=(item.get("institution") or "").strip()[:200],
            graduation_year=_valid_year(item.get("graduation_year")),
            defaults={
                "field_of_study": (item.get("field_of_study") or "").strip()[:200],
                "level": _closest_education_level(item.get("level")),
                "evidence": span,
                "confidence": _confidence(span),
            },
        )


def _persist_languages(candidate: Candidate, items: list[dict], span_for) -> None:
    for item in items:
        language = (item.get("language") or "").strip()
        if not language:
            continue
        level = (item.get("level") or "").strip().upper()
        if level not in VALID_LANGUAGE_LEVELS:
            level = LanguageLevel.B1
        span = span_for(item)
        CandidateLanguage.objects.update_or_create(
            candidate=candidate,
            language=language[:60],
            defaults={"level": level, "evidence": span, "confidence": _confidence(span)},
        )


def _persist_certifications(candidate: Candidate, items: list[dict], span_for) -> None:
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        span = span_for(item)
        Certification.objects.update_or_create(
            candidate=candidate,
            name=name[:200],
            defaults={
                "issuer": (item.get("issuer") or "").strip()[:200],
                "obtained_year": _valid_year(item.get("obtained_year")),
                "evidence": span,
                "confidence": _confidence(span),
            },
        )


# --- Utilitaires -----------------------------------------------------------
# Le schema demande le format AAAA-MM, mais un modele recopie souvent la forme
# lue dans le document. Sur des CV francophones, le modele vision rendait
# « 06/2021 » : la date etait rejetee, l'experience enregistree sans periode,
# et l'anciennete totale tombait a zero — trois cas sur cinq du jeu
# d'evaluation. Mieux vaut accepter les formes courantes que perdre la donnee.
_DATE_PATTERNS = (
    re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})"),        # 2021-06
    re.compile(r"^(?P<month>\d{1,2})/(?P<year>\d{4})"),        # 06/2021
    re.compile(r"^(?P<month>\d{1,2})-(?P<year>\d{4})"),        # 06-2021
    re.compile(r"^(?P<month>\d{1,2})\.(?P<year>\d{4})"),       # 06.2021
    re.compile(r"^(?P<year>\d{4})$"),                          # 2021
)

_MONTH_NAMES = {
    "janvier": 1, "january": 1, "jan": 1, "fevrier": 2, "february": 2, "feb": 2,
    "mars": 3, "march": 3, "mar": 3, "avril": 4, "april": 4, "apr": 4,
    "mai": 5, "may": 5, "juin": 6, "june": 6, "jun": 6,
    "juillet": 7, "july": 7, "jul": 7, "aout": 8, "august": 8, "aug": 8,
    "septembre": 9, "september": 9, "sep": 9, "sept": 9,
    "octobre": 10, "october": 10, "oct": 10,
    "novembre": 11, "november": 11, "nov": 11,
    "decembre": 12, "december": 12, "dec": 12,
}
_NAMED_DATE = re.compile(r"^(?P<name>[a-z]+)\.?\s+(?P<year>\d{4})")


def _parse_month(value: object) -> dt.date | None:
    """Convertit une date de CV en date. Renvoie None si inexploitable.

    Formats acceptes : AAAA-MM, MM/AAAA, MM-AAAA, MM.AAAA, AAAA, et
    « juin 2021 » ou « June 2021 ».
    """
    if not value:
        return None
    text = str(value).strip().lower()

    for pattern in _DATE_PATTERNS:
        match = pattern.match(text)
        if match:
            groups = match.groupdict()
            return _safe_date(int(groups["year"]), int(groups.get("month") or 1))

    named = _NAMED_DATE.match(_strip_accents(text))
    if named:
        month = _MONTH_NAMES.get(named.group("name"))
        if month:
            return _safe_date(int(named.group("year")), month)
    return None


def _safe_date(year: int, month: int) -> dt.date | None:
    if not 1900 <= year <= dt.date.today().year + 1 or not 1 <= month <= 12:
        return None
    return dt.date(year, month, 1)


def _strip_accents(text: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _valid_year(value: object) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= dt.date.today().year + 1 else None


def _closest_education_level(value: object) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return EducationLevel.NONE
    return min(VALID_EDUCATION_LEVELS, key=lambda valid: abs(valid - level))


def _clean_url(value: object) -> str:
    url = (value or "").strip() if isinstance(value, str) else ""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url[:200]


def _total_experience_years(candidate: Candidate) -> float:
    """Somme des periodes travaillees, chevauchements fusionnes.

    Additionner naivement la duree de chaque poste surestime l'experience des
    profils qui ont cumule des missions en parallele. On fusionne donc les
    intervalles avant de sommer.
    """
    periods: list[tuple[dt.date, dt.date]] = []
    today = dt.date.today()
    for experience in candidate.experiences.all():
        if not experience.start_date:
            continue
        end = experience.end_date or today
        if end > experience.start_date:
            periods.append((experience.start_date, end))

    if not periods:
        return 0.0

    periods.sort()
    merged = [periods[0]]
    for start, end in periods[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    days = sum((end - start).days for start, end in merged)
    return days / 365.25
