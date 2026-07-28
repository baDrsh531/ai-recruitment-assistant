"""Reglages de developpement local."""

from .base import *  # noqa: F403
from .base import INSTALLED_APPS, MIDDLEWARE, STORAGES, env

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Le hachage manifeste de WhiteNoise casse `runserver` sans collectstatic.
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

try:
    import debug_toolbar  # noqa: F401
except ImportError:
    pass
else:
    INSTALLED_APPS += ["debug_toolbar"]
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
    INTERNAL_IPS = ["127.0.0.1"]
    # La barre de debogage recouvre le tiers droit de la fenetre : elle rend
    # toute capture d'ecran inexploitable. `SHOW_DEBUG_TOOLBAR=False` la
    # desactive sans quitter le mode developpement.
    DEBUG_TOOLBAR_CONFIG = {
        "SHOW_TOOLBAR_CALLBACK": lambda request: env.bool(
            "SHOW_DEBUG_TOOLBAR", default=True
        ),
    }
