"""Tests de ce que le deploiement suppose.

Une configuration de production ne se verifie pas en production. Ces tests
echouent ici plutot que la-bas : sonde de sante, reglages de securite, et le
bandeau de demonstration qui doit dire ce que la demonstration ne peut pas
montrer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.urls import reverse

from apps.matching.engine import ENGINE_VERSION

RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


# --- Sonde de sante ----------------------------------------------------------
def test_the_health_probe_answers_without_a_session(client, db):
    """L'hebergeur interroge la sonde avant qu'aucun compte n'existe."""
    reponse = client.get(reverse("sante"))

    assert reponse.status_code == 200
    donnees = json.loads(reponse.content)
    assert donnees["status"] == "ok"
    assert donnees["moteur"] == ENGINE_VERSION


def test_the_health_probe_touches_the_database(client, db, monkeypatch):
    """Repondre 200 sans toucher la base ferait passer un service casse pour sain.

    On remplace la connexion vue par la sonde, pas la connexion globale : cette
    derniere sert encore au demontage du test, et la casser ferait echouer la
    suite pour une raison sans rapport avec ce qu'on mesure.
    """
    from apps.core import views

    class ConnexionCassee:
        def cursor(self):
            raise RuntimeError("base injoignable")

    monkeypatch.setattr(views, "connection", ConnexionCassee())
    reponse = client.get(reverse("sante"))

    assert reponse.status_code == 503
    donnees = json.loads(reponse.content)
    assert donnees["status"] == "degrade"
    assert donnees["base"] == "injoignable"


def test_the_health_probe_leaks_no_business_data(client, db):
    contenu = client.get(reverse("sante")).content.decode()
    assert "candidat" not in contenu.lower()
    assert "SECRET" not in contenu


# --- Bandeau de demonstration ------------------------------------------------
def test_the_banner_is_absent_outside_demo_mode(client, db, recruteur, settings):
    settings.DEMO_MODE = False
    client.force_login(recruteur)
    contenu = client.get(reverse("candidates:dashboard")).content.decode()

    assert "Demonstration publique" not in contenu


def test_the_banner_states_what_cannot_be_shown(client, db, recruteur, settings):
    settings.DEMO_MODE = True
    settings.LLM_BASE_URL = ""
    client.force_login(recruteur)
    contenu = client.get(reverse("candidates:dashboard")).content.decode()

    assert "Demonstration publique" in contenu
    assert "indisponibles" in contenu
    # Sans apostrophe dans l'assertion : Django les echappe, et un test qui
    # depend de la forme de l'echappement casse au premier changement de moteur
    # de gabarit sans qu'aucun comportement n'ait bouge.
    assert "serveur prive" in contenu
    assert "jamais eu besoin" in contenu


def test_the_banner_drops_the_caveat_when_the_model_is_reachable(
    client, db, recruteur, settings
):
    settings.DEMO_MODE = True
    settings.LLM_BASE_URL = "http://192.168.0.64:30000/v1"
    client.force_login(recruteur)
    contenu = client.get(reverse("candidates:dashboard")).content.decode()

    assert "Demonstration publique" in contenu
    assert "indisponibles" not in contenu


# --- Fichiers de deploiement -------------------------------------------------
def test_the_render_manifest_exists_and_declares_the_probe():
    manifeste = (RACINE / "render.yaml").read_text(encoding="utf-8")

    assert "healthCheckPath: /sante/" in manifeste
    assert "config.settings.prod" in manifeste
    assert "gunicorn" in manifeste


def test_the_manifest_does_not_ship_a_secret():
    """La cle est generee par l'hebergeur, jamais ecrite dans le depot."""
    manifeste = (RACINE / "render.yaml").read_text(encoding="utf-8")

    assert "generateValue: true" in manifeste
    assert "dev-only-change-me" not in manifeste


def test_the_build_script_stops_on_the_first_failure():
    """Mieux vaut ne pas deployer qu'exposer une version a moitie migree."""
    script = (RACINE / "scripts" / "build.sh").read_text(encoding="utf-8")

    assert "set -o errexit" in script
    assert "migrate --no-input" in script
    assert "collectstatic --no-input" in script


def test_production_settings_are_strict(settings):
    """Ce qui protege une application en ligne, verifie hors ligne."""
    import importlib

    prod = importlib.import_module("config.settings.prod")

    assert prod.DEBUG is False
    assert prod.SESSION_COOKIE_SECURE is True
    assert prod.CSRF_COOKIE_SECURE is True
    assert prod.SECURE_HSTS_SECONDS > 0
    assert prod.X_FRAME_OPTIONS == "DENY"
    assert prod.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
