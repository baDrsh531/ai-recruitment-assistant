from django.urls import path

from .views import (
    ConsentView,
    DraftView,
    LogView,
    SendView,
    SilenceView,
    ThreadView,
)

app_name = "outreach"

urlpatterns = [
    path("", SilenceView.as_view(), name="silence"),
    path("candidature/<uuid:pk>/", ThreadView.as_view(), name="thread"),
    path("candidature/<uuid:pk>/rediger/", DraftView.as_view(), name="draft"),
    path("candidature/<uuid:pk>/consigner/", LogView.as_view(), name="log"),
    path("candidature/<uuid:pk>/consentement/", ConsentView.as_view(), name="consent"),
    path("message/<uuid:pk>/envoyer/", SendView.as_view(), name="send"),
]
