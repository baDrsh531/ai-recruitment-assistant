from django.urls import path

from .views import AskView, AssistantView, ClearHistoryView

app_name = "assistant"

urlpatterns = [
    path("<slug:slug>/assistant/", AssistantView.as_view(), name="offer"),
    path("<slug:slug>/assistant/demander/", AskView.as_view(), name="ask"),
    path("<slug:slug>/assistant/effacer/", ClearHistoryView.as_view(), name="clear"),
]
