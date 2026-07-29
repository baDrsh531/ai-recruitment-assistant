from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ApplicationViewSet, CandidateViewSet, OfferViewSet, racine

app_name = "api"

router = DefaultRouter()
router.register("offres", OfferViewSet)
router.register("candidats", CandidateViewSet)
router.register("candidatures", ApplicationViewSet)

urlpatterns = [
    path("", racine, name="root"),
    path("", include(router.urls)),
]
