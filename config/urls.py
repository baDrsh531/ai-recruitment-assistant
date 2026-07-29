from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("comptes/", include("apps.accounts.urls", namespace="accounts")),
    # Le classement est enregistre avant les URLs d'offres : sinon le motif
    # <slug>/ de jobs capturerait « <slug>/classement/ ».
    path("offres/", include("apps.assistant.urls", namespace="assistant")),
    path("offres/", include("apps.matching.urls", namespace="matching")),
    path("offres/", include("apps.jobs.urls", namespace="jobs")),
    path("cv/", include("apps.parsing.urls", namespace="parsing")),
    path("transparence/", include("apps.evaluation.urls", namespace="evaluation")),
    path("api/", include("apps.api.urls", namespace="api")),
    path("", include("apps.candidates.urls", namespace="candidates")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
