"""API REST.

Elle etait declaree — DRF installe, authentification, permissions et pagination
configurees, mentionnee dans le schema d'architecture — et ne comportait aucune
route. Elle en a maintenant, et elle passe par les memes services que
l'interface : meme validation de decision, meme controle de role, meme journal
d'audit, meme screening a l'aveugle. Une API qui doublerait ces regles finirait
par en appliquer d'autres.

Lecture seule sur les ressources ; les deux seules ecritures sont celles qui
existent deja a l'ecran : faire avancer une candidature, et relancer le calcul
des scores d'une offre.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.reverse import reverse

from apps.ai.client import InferenceError
from apps.assistant import textsearch
from apps.candidates.models import Application, Candidate
from apps.jobs.models import JobOffer
from apps.matching import counterfactual as cf
from apps.matching.services import DecisionRefused, decide, latest_scores, score_offer

from .permissions import ReadOnlyOrCanDecide
from .serializers import (
    ApplicationSerializer,
    CandidateSerializer,
    DecisionSerializer,
    JobOfferSerializer,
    RankingEntrySerializer,
)


class OfferViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = JobOffer.objects.prefetch_related("skills").order_by("-created_at")
    serializer_class = JobOfferSerializer
    permission_classes = [ReadOnlyOrCanDecide]
    lookup_field = "slug"

    @action(detail=True, methods=["get"], url_path="classement")
    def ranking(self, request, slug=None):
        """Classement de l'offre : dernier score de chaque candidature.

        Le rang est calcule ici plutot que laisse au consommateur : deux
        clients qui trieraient eux-memes finiraient par ne pas trier pareil,
        et le classement est precisement ce que le systeme doit garantir
        reproductible.
        """
        offre = self.get_object()
        scores = latest_scores(offre)
        entrees = [
            {
                "rank": rang,
                "application_id": score.application_id,
                "candidate": score.application.candidate,
                "stage": score.application.stage,
                "percentage": score.percentage,
                "effective_score": score.effective_score,
                "is_overridden": score.is_overridden,
                "gaps": [gap["skill"] for gap in score.gaps],
            }
            for rang, score in enumerate(scores, start=1)
        ]
        serializer = RankingEntrySerializer(
            entrees, many=True, context=self.get_serializer_context()
        )
        return Response(
            {
                "offer": offre.slug,
                # Versions distinctes = classement heterogene : deux
                # candidatures calculees par des moteurs differents ne sont pas
                # comparables, et le consommateur doit pouvoir s'en apercevoir.
                "engine_versions": sorted({score.engine_version for score in scores}),
                "count": len(entrees),
                "unscored": offre.applications.count() - len(entrees),
                "results": serializer.data,
            }
        )

    @action(detail=True, methods=["post"], url_path="scorer")
    def rescore(self, request, slug=None):
        """Relance le calcul pour toutes les candidatures ouvertes de l'offre.

        `explication=1` demande en plus l'analyse redigee. Elle depend d'un
        serveur d'inference : s'il est injoignable, les scores sont renvoyes
        quand meme et le champ `explained` dit que le commentaire manque.
        """
        offre = self.get_object()
        avec_analyse = str(request.data.get("explication", "")) in {"1", "true", "True"}
        explique = avec_analyse

        try:
            scores = score_offer(offre, with_explanation=avec_analyse, actor=request.user)
        except InferenceError as exc:
            scores = latest_scores(offre)
            explique = False
            detail = str(exc)
        else:
            detail = ""

        return Response(
            {
                "offer": offre.slug,
                "scored": len(scores),
                "explained": explique and any(score.explanation for score in scores),
                "semantic_used": all(score.semantic_used for score in scores) if scores else False,
                "detail": detail,
            }
        )


class CandidateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Candidate.objects.prefetch_related("skills").order_by("full_name")
    serializer_class = CandidateSerializer
    permission_classes = [ReadOnlyOrCanDecide]

    @action(detail=False, methods=["get"], url_path="recherche")
    def search(self, request):
        """Recherche plein texte sur les profils.

        `q` porte la requete, `limite` borne le nombre de resultats. Le
        classement vient de BM25, eventuellement fusionne au vectoriel par
        rang : aucun modele de langage n'intervient et deux appels identiques
        renvoient la meme liste dans le meme ordre.
        """
        requete = request.query_params.get("q", "").strip()
        if not requete:
            return Response(
                {"detail": "Le parametre « q » est obligatoire."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            limite = min(50, max(1, int(request.query_params.get("limite", 10))))
        except ValueError:
            return Response(
                {"detail": "Le parametre « limite » doit etre un entier."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resultat = textsearch.search(requete, limit=limite)
        contexte = self.get_serializer_context()
        return Response(
            {
                **resultat.as_dict(),
                "results": [
                    {
                        **hit.as_dict(),
                        "candidate": CandidateSerializer(
                            hit.candidate, context=contexte
                        ).data,
                    }
                    for hit in resultat.hits
                ],
            }
        )


class ApplicationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = (
        Application.objects.select_related("candidate", "offer")
        .prefetch_related("candidate__skills")
        .order_by("-applied_at")
    )
    serializer_class = ApplicationSerializer
    permission_classes = [ReadOnlyOrCanDecide]

    def get_queryset(self):
        queryset = super().get_queryset()
        offre = self.request.query_params.get("offre")
        if offre:
            queryset = queryset.filter(offer__slug=offre)
        etape = self.request.query_params.get("etape")
        if etape:
            queryset = queryset.filter(stage=etape)
        return queryset

    @action(detail=True, methods=["get"], url_path="ecarts")
    def counterfactual(self, request, pk=None):
        """Ce qu'il manque a ce candidat pour atteindre le seuil.

        `seuil` accepte une valeur entre 0 et 1. Le calcul est deterministe et
        ne consulte aucun modele : il rejoue le moteur sur une copie du profil,
        jamais sur le dossier lui-meme.
        """
        candidature = self.get_object()
        try:
            seuil = float(request.query_params.get("seuil", cf.DEFAULT_TARGET))
        except ValueError:
            return Response(
                {"detail": "Le parametre « seuil » doit etre un nombre entre 0 et 1."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not 0 < seuil <= 1:
            return Response(
                {"detail": "Le parametre « seuil » doit etre compris entre 0 et 1."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rapport = cf.analyse(candidature.candidate, candidature.offer, target=seuil)
        return Response(
            {
                "application": str(candidature.pk),
                "offer": candidature.offer.slug,
                **rapport.as_dict(),
                "note": (
                    "La localisation n'est jamais proposee comme levier. Une "
                    "competence absente du profil peut aussi n'avoir pas ete "
                    "extraite du CV."
                ),
            }
        )

    @action(detail=True, methods=["post"], url_path="decider")
    def decide(self, request, pk=None):
        """Fait avancer une candidature.

        Meme chemin que le bouton de l'interface : la validation, l'imputation
        et le journal viennent de `services.decide`. Ecarter un candidat exige
        toujours un motif ecrit, y compris depuis un client automatise — c'est
        precisement la ou la garantie compte le plus.
        """
        candidature = get_object_or_404(
            Application.objects.select_related("candidate", "offer"), pk=pk
        )
        entree = DecisionSerializer(data=request.data)
        entree.is_valid(raise_exception=True)

        try:
            decide(
                candidature,
                stage=entree.validated_data["stage"],
                note=entree.validated_data.get("note", ""),
                actor=request.user,
                request=request,
            )
        except DecisionRefused as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(candidature)
        return Response(serializer.data)


@api_view(["GET"])
@permission_classes([ReadOnlyOrCanDecide])
def racine(request):
    """Point d'entree : ce que l'API expose, et ce qu'elle n'expose pas."""
    return Response(
        {
            "offres": reverse("api:joboffer-list", request=request),
            "candidats": reverse("api:candidate-list", request=request),
            "recherche": reverse("api:candidate-search", request=request) + "?q=",
            "candidatures": reverse("api:application-list", request=request),
            "note": (
                "Lecture ouverte a tout compte authentifie ; les ecritures "
                "supposent un compte habilite et sont journalisees. Le "
                "screening a l'aveugle du compte appelant s'applique aux "
                "reponses : les identifiants directs sont retires, et le champ "
                "« blind » indique quand c'est le cas."
            ),
        }
    )
