from django.urls import path

from .views import ApplicationDetailView, CandidateDetailView, CandidateListView, DashboardView

app_name = "candidates"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("candidats/", CandidateListView.as_view(), name="list"),
    path("candidats/<uuid:pk>/", CandidateDetailView.as_view(), name="detail"),
    path("candidatures/<uuid:pk>/", ApplicationDetailView.as_view(), name="application_detail"),
]
