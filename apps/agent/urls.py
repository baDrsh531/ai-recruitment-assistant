from django.urls import path

from .views import (
    AgentDashboardView,
    ResolveRecommendationView,
    RunAgentView,
    WatchView,
)

app_name = "agent"

urlpatterns = [
    path("", AgentDashboardView.as_view(), name="dashboard"),
    path("lancer/", RunAgentView.as_view(), name="run"),
    path("veille/", WatchView.as_view(), name="watch"),
    path(
        "recommandations/<uuid:pk>/trancher/",
        ResolveRecommendationView.as_view(),
        name="resolve",
    ),
]
