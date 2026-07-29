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
    path("", include("apps.candidates.urls", namespace="candidates")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# La barre de debogage s'injecte dans chaque reponse et y insere ses propres
# URLs : sans cet enregistrement, tout rendu de page echoue.
#
# Le critere est bien « l'application est installee », et non `settings.DEBUG`.
# L'URLconf n'est importee qu'une fois : la conditionner a DEBUG la figerait a
# la valeur du moment de l'import, et activer DEBUG plus tard (tests, shell)
# casserait toutes les pages. En production, `debug_toolbar` n'est jamais dans
# INSTALLED_APPS, donc ce bloc ne s'execute pas.
if "debug_toolbar" in settings.INSTALLED_APPS:
    urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
