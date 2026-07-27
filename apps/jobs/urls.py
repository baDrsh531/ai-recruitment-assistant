from django.urls import path

from .views import JobOfferDetailView, JobOfferListView

app_name = "jobs"

urlpatterns = [
    path("", JobOfferListView.as_view(), name="list"),
    path("<slug:slug>/", JobOfferDetailView.as_view(), name="detail"),
]
