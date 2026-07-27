from django.urls import path

from .views import OfferRankingView, ScoreOfferView

app_name = "matching"

urlpatterns = [
    path("<slug:slug>/classement/", OfferRankingView.as_view(), name="ranking"),
    path("<slug:slug>/scorer/", ScoreOfferView.as_view(), name="score_offer"),
]
