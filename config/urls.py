from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.core.views import sante

urlpatterns = [
    path("sante/", sante, name="sante"),
    path("admin/", admin.site.urls),
    # Bascule de langue de Django : elle enregistre le choix en session et
    # renvoie ou l'on etait. Pas de prefixe d'URL par langue — le contenu
    # metier reste le meme, seule l'interface change.
    path("i18n/", include("django.conf.urls.i18n")),
    path("comptes/", include("apps.accounts.urls", namespace="accounts")),
    # Le classement est enregistre avant les URLs d'offres : sinon le motif
    # <slug>/ de jobs capturerait « <slug>/classement/ ».
    path("offres/", include("apps.assistant.urls", namespace="assistant")),
    path("offres/", include("apps.matching.urls", namespace="matching")),
    path("offres/", include("apps.jobs.urls", namespace="jobs")),
    path("cv/", include("apps.parsing.urls", namespace="parsing")),
    path("transparence/", include("apps.evaluation.urls", namespace="evaluation")),
    path("api/", include("apps.api.urls", namespace="api")),
    path("agent/", include("apps.agent.urls", namespace="agent")),
    path("echanges/", include("apps.outreach.urls", namespace="outreach")),
    path("", include("apps.candidates.urls", namespace="candidates")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
