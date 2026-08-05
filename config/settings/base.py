"""Reglages communs a tous les environnements."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# --- Securite -------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", default="dev-only-change-me")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# Demonstration publique. Le mode ne change aucun comportement du moteur : il
# affiche un bandeau disant ce que la demonstration peut montrer et ce qu'elle
# ne peut pas, plutot que de laisser un visiteur conclure qu'une fonctionnalite
# est cassee alors qu'elle demande un serveur d'inference prive.
DEMO_MODE = env.bool("DEMO_MODE", default=False)

# --- Agent d'orchestration ------------------------------------------------
# Interrupteur d'arret. Desactive par defaut : un systeme qui appelle un modele
# de langage tout seul ne doit pas se mettre en marche parce qu'on a deploye.
AGENT_ENABLED = env.bool("AGENT_ENABLED", default=False)

# Plafond dur de tokens sur la journee glissante, entree et sortie confondues.
# 0 = pas de limite, a n'utiliser qu'en developpement. Le mode d'echec le plus
# courant n'est pas la depense volontaire, c'est la boucle de reprise.
AGENT_DAILY_TOKEN_BUDGET = env.int("AGENT_DAILY_TOKEN_BUDGET", default=200_000)

# Compte sous lequel l'agent agit. Son role le place hors de `can_decide` :
# c'est la garantie structurelle qu'il ne peut pas trancher.
AGENT_USERNAME = env("AGENT_USERNAME", default="agent")
DEMO_READONLY_USERNAME = env("DEMO_READONLY_USERNAME", default="observateur")

# --- Echanges avec les candidats -------------------------------------------
# Nom qui signe les messages, et delai de reponse annonce dans les gabarits.
# Annoncer un delai engage : `apps/outreach/silence.py` mesure ensuite les
# dossiers restes muets au-dela.
# Vide = on retombe sur le nom de la marque (voir apps/core/brand.py). Mettre
# « notre equipe » en defaut donnait un pied de courriel qui se lisait mal.
OUTREACH_ORGANISATION = env("OUTREACH_ORGANISATION", default="")
OUTREACH_RESPONSE_DAYS = env.int("OUTREACH_RESPONSE_DAYS", default=15)

# --- Applications ---------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.humanize",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "django_extensions",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.ai",
    "apps.jobs",
    "apps.candidates",
    "apps.parsing",
    "apps.matching",
    "apps.assistant",
    "apps.evaluation",
    "apps.api",
    "apps.agent",
    "apps.outreach",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.demonstration",
            ],
        },
    },
]

# --- Base de donnees ------------------------------------------------------
DATABASES = {
    "default": env.db_url("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentification -----------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "accounts:login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalisation -------------------------------------------------
LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True

# --- Fichiers statiques et media ------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Taille maximale d'un CV depose (10 Mo).
MAX_CV_SIZE_BYTES = 10 * 1024 * 1024
ALLOWED_CV_EXTENSIONS = [".pdf", ".docx"]

# --- Celery ---------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="")
CELERY_TASK_ALWAYS_EAGER = not CELERY_BROKER_URL
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_RESULT_BACKEND = CELERY_BROKER_URL or None
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE

# La purge des dossiers arrives a echeance doit tourner sans qu'on y pense :
# une obligation de conservation limitee qui depend d'une commande lancee a la
# main n'est pas respectee.
CELERY_BEAT_SCHEDULE = {
    "purge-dossiers-expires": {
        "task": "apps.candidates.tasks.purge_expired_task",
        "schedule": 24 * 60 * 60,
    },
    # Meme raisonnement pour la surveillance du biais : un controle qui depend
    # d'une page qu'un responsable doit penser a ouvrir ne se declenche jamais
    # entre deux audits. Cette tache n'appelle aucun modele et ne consomme donc
    # rien ; la programmer plus souvent ne coute que du temps de calcul.
    "veille-derive-biais": {
        "task": "apps.agent.tasks.watch_task",
        "schedule": 12 * 60 * 60,
    },
}

# --- Couche IA ------------------------------------------------------------
# Deux endpoints distincts : un modele texte et un modele vision-langage.
# Les adresses viennent exclusivement du .env : aucune infrastructure n'est
# codee en dur ici. `python manage.py check_ai` signale une configuration
# manquante ou erronee.
LLM = {
    "BASE_URL": env("LLM_BASE_URL", default=""),
    "MODEL": env("LLM_MODEL", default=""),
    "API_KEY": env("LLM_API_KEY", default="not-needed"),
    "TIMEOUT": env.int("LLM_TIMEOUT", default=120),
}
VLM = {
    "BASE_URL": env("VLM_BASE_URL", default=""),
    "MODEL": env("VLM_MODEL", default=""),
    "API_KEY": env("VLM_API_KEY", default="not-needed"),
    "TIMEOUT": env.int("VLM_TIMEOUT", default=180),
}
# Rapprochement semantique des competences.
#
# Desactive par defaut, apres mesure. Un modele de phrases generaliste n'a
# aucune connaissance technique : sur les paires de reference de
# `python manage.py probe_semantic`, il note « Kubernetes / Boulangerie » a
# 0.827 — au-dessus de toutes les paires reellement proches — et « Symfony /
# Laravel » a 0.393. Les deux populations se chevauchent : aucun seuil ne les
# separe. Active, cette couche crediterait un boulanger sur une exigence
# Kubernetes.
#
# Le code reste en place : il redeviendra utile avec un modele entraine sur
# une taxonomie de competences. En attendant, l'ontologie fait le travail, et
# elle est inspectable.
EMBEDDING = {
    "PROVIDER": env("EMBEDDING_PROVIDER", default="none"),  # none | local | server
    "MODEL": env(
        "EMBEDDING_MODEL",
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ),
    "DIM": env.int("EMBEDDING_DIM", default=384),
}

# --- Conformite AI Act / RGPD ---------------------------------------------
DATA_RETENTION_DAYS = env.int("DATA_RETENTION_DAYS", default=365)
BLIND_SCREENING_DEFAULT = env.bool("BLIND_SCREENING_DEFAULT", default=False)

# --- REST Framework -------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

# --- Journalisation -------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "apps": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "django.db.backends": {"level": "WARNING"},
    },
}
