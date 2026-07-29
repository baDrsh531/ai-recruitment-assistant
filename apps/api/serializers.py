"""Representations JSON des objets du domaine.

Deux principes tiennent tout le fichier :

1. **Le screening a l'aveugle vaut ici aussi.** Une API qui renverrait ce que
   l'interface masque ferait de l'attenuation du biais une formalite : il
   suffirait d'appeler `/api/candidats/` pour retrouver les noms. Le masquage
   est donc applique dans le serializer, a partir de la preference du compte
   qui appelle.
2. **Aucune ecriture ne passe par un serializer.** Faire avancer une
   candidature ou lancer un calcul reste l'affaire des services du domaine
   (`decide`, `score_application`), qui valident, imputent et journalisent.
   L'API est une porte d'entree, pas un second jeu de regles.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.jobs.models import JobOffer, JobSkill
from apps.matching.models import MatchScore


def _blind(context) -> bool:
    """Preference de screening du compte appelant."""
    request = context.get("request")
    utilisateur = getattr(request, "user", None)
    return bool(getattr(utilisateur, "blind_screening", False))


class JobSkillSerializer(serializers.ModelSerializer):
    requirement = serializers.CharField(source="get_requirement_display")

    class Meta:
        model = JobSkill
        fields = ["name", "normalized_name", "requirement", "weight", "min_years"]


class JobOfferSerializer(serializers.ModelSerializer):
    status = serializers.CharField(source="get_status_display")
    skills = JobSkillSerializer(many=True, read_only=True)
    applications_count = serializers.IntegerField(source="applications.count", read_only=True)

    class Meta:
        model = JobOffer
        fields = [
            "id", "slug", "title", "department", "location", "status",
            "experience_min_years", "education_level", "remote_policy",
            "contract_type", "blind_screening", "weights", "skills",
            "applications_count", "created_at",
        ]


class CandidateSkillSerializer(serializers.ModelSerializer):
    source = serializers.CharField(source="get_source_display")

    class Meta:
        model = CandidateSkill
        fields = ["name", "normalized_name", "years", "last_used_year", "source"]


class CandidateSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    skills = CandidateSkillSerializer(many=True, read_only=True)
    days_until_purge = serializers.IntegerField(read_only=True)
    blind = serializers.SerializerMethodField()

    class Meta:
        model = Candidate
        fields = [
            "id", "full_name", "headline", "email", "phone", "location",
            "linkedin_url", "github_url", "total_experience_years",
            "highest_education", "skills", "retention_until",
            "days_until_purge", "blind", "created_at",
        ]

    def get_full_name(self, candidat: Candidate) -> str:
        return candidat.display_name(blind=_blind(self.context))

    def get_blind(self, candidat: Candidate) -> bool:
        return _blind(self.context)

    def to_representation(self, candidat: Candidate) -> dict:
        """Retire les identifiants directs en mode aveugle.

        Masquer le nom tout en renvoyant l'adresse e-mail et le profil LinkedIn
        ne masquerait rien du tout. Le champ est vide, et `blind` dit pourquoi
        — un consommateur doit pouvoir distinguer « absent » de « retire ».
        """
        donnees = super().to_representation(candidat)
        if _blind(self.context):
            for champ in ["email", "phone", "location", "linkedin_url", "github_url"]:
                donnees[champ] = ""
        return donnees


class MatchScoreSerializer(serializers.ModelSerializer):
    percentage = serializers.IntegerField(read_only=True)
    effective_score = serializers.FloatField(read_only=True)
    is_overridden = serializers.BooleanField(read_only=True)

    class Meta:
        model = MatchScore
        fields = [
            "id", "overall", "effective_score", "percentage", "is_overridden",
            "engine_version", "weights_used", "breakdown", "skill_matches",
            "gaps", "semantic_used", "blind", "compute_ms", "explanation",
            "explanation_model", "explanation_prompt_id",
            "explanation_prompt_version", "created_at",
        ]


class ApplicationSerializer(serializers.ModelSerializer):
    candidate = CandidateSerializer(read_only=True)
    offer = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    stage_display = serializers.CharField(source="get_stage_display", read_only=True)
    decided_by = serializers.StringRelatedField(read_only=True)
    latest_score = serializers.SerializerMethodField()

    class Meta:
        model = Application
        fields = [
            "id", "candidate", "offer", "stage", "stage_display", "is_closed",
            "decided_by", "decided_at", "decision_note", "applied_at",
            "latest_score",
        ]

    def get_latest_score(self, candidature: Application) -> dict | None:
        score = candidature.scores.order_by("-created_at").first()
        if score is None:
            return None
        return {
            "percentage": score.percentage,
            "engine_version": score.engine_version,
            "computed_at": score.created_at,
        }


class RankingEntrySerializer(serializers.Serializer):
    """Une ligne de classement : le candidat, son rang et son score."""

    rank = serializers.IntegerField()
    application_id = serializers.UUIDField()
    candidate = CandidateSerializer()
    stage = serializers.CharField()
    percentage = serializers.IntegerField()
    effective_score = serializers.FloatField()
    is_overridden = serializers.BooleanField()
    gaps = serializers.ListField(child=serializers.CharField())


class DecisionSerializer(serializers.Serializer):
    """Entree de `POST /candidatures/{id}/decider/`.

    La validation de fond — motif obligatoire pour ecarter, habilitation du
    compte — reste dans `services.decide`. Ce serializer ne verifie que la
    forme, pour renvoyer une erreur 400 lisible plutot qu'une exception.
    """

    stage = serializers.ChoiceField(choices=Application.Stage.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="")
