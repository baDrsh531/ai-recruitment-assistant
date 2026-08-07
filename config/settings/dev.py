"""Reglages de developpement local."""

from .base import *  # noqa: F403
from .base import STORAGES

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Le hachage manifeste de WhiteNoise casse `runserver` sans collectstatic.
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

# Console par defaut. Renseigner `EMAIL_HOST` dans le `.env` bascule sur SMTP —
# `base.py` s'en charge, et la condition ci-dessous evite que ce fichier ne
# reprenne la main dessus.
if not EMAIL_HOST:  # noqa: F405
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# django-debug-toolbar a ete retiree. Elle recouvrait le tiers droit de chaque
# page et son enregistrement d'URLs, oublie, avait un jour casse toutes les
# pages de developpement — un mode de panne que la suite de tests, qui tourne
# avec DEBUG=False, ne voyait pas. Le compte de requetes SQL reste accessible
# par `django.db.connection.queries` dans un shell, et la page « Appels
# modele » couvre deja la latence, ce que la barre mesurait le moins bien.
