from django.urls import path

from .views import (
    ApplicationDetailView,
    CandidateDetailView,
    CandidateListView,
    DashboardView,
    DuplicateListView,
    ExportApplicationView,
    ExportCandidateExplanationView,
    MergeCandidatesView,
)

app_name = "candidates"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("candidats/", CandidateListView.as_view(), name="list"),
    path("candidats/doublons/", DuplicateListView.as_view(), name="duplicates"),
    path("candidats/fusionner/", MergeCandidatesView.as_view(), name="merge"),
    path("candidats/<uuid:pk>/", CandidateDetailView.as_view(), name="detail"),
    path("candidatures/<uuid:pk>/", ApplicationDetailView.as_view(), name="application_detail"),
    path(
        "candidatures/<uuid:pk>/dossier.pdf",
        ExportApplicationView.as_view(),
        name="export_application",
    ),
    path(
        "candidatures/<uuid:pk>/explication-candidat.pdf",
        ExportCandidateExplanationView.as_view(),
        name="export_candidate_explanation",
    ),
]
