from django.urls import path

from .views import (
    ComparisonView,
    GenerateQuestionsView,
    OfferRankingView,
    ScoreOfferView,
)

app_name = "matching"

urlpatterns = [
    path("<slug:slug>/classement/", OfferRankingView.as_view(), name="ranking"),
    path("<slug:slug>/comparer/", ComparisonView.as_view(), name="comparison"),
    path("<slug:slug>/scorer/", ScoreOfferView.as_view(), name="score_offer"),
    path(
        "candidatures/<uuid:pk>/questions/",
        GenerateQuestionsView.as_view(),
        name="generate_questions",
    ),
]
