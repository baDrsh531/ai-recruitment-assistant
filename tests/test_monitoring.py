"""Tests de la surveillance continue du biais.

Le module transforme une photographie en releve. Ce qui est teste : qu'il sait
comparer au releve precedent, qu'il distingue l'ecart legal de la derive, qu'il
n'invente pas de comparaison au premier passage, et qu'il ne bloque rien.
"""

from __future__ import annotations

import itertools

import pytest
from django.urls import reverse

from apps.core.models import AuditLog
from apps.evaluation import bias, monitoring
from apps.matching import engine


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


_HORODATAGE = itertools.count()


def _releve(ratios: dict[str, float]) -> AuditLog:
    """Ecrit un releve directement, pour eprouver la comparaison sans
    relancer l'audit complet a chaque fois.

    L'horodatage est pose explicitement, une seconde par appel. L'horloge de la
    machine avance par paliers de ~15 ms : deux releves ecrits a la suite
    porteraient le meme `created_at`, et leur ordre serait arbitraire. Le test
    porte sur la lecture de l'historique, pas sur la vitesse d'ecriture.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.core.services import record_audit

    entree = record_audit(
        AuditLog.Action.BIAS_MONITORED,
        summary="releve de test",
        ratios=ratios,
        engine_version="1.2.0",
    )
    AuditLog.objects.filter(pk=entree.pk).update(
        created_at=timezone.now() + timedelta(seconds=next(_HORODATAGE))
    )
    entree.refresh_from_db()
    return entree


# --- Alertes -----------------------------------------------------------------
def test_a_ratio_below_the_legal_threshold_raises_a_legal_alert():
    alerte = monitoring._alerter("localisation", 0.62, 0.81)

    assert alerte.niveau == "ecart_legal"
    assert "quatre cinquiemes" in alerte.message
    assert "screening a l'aveugle" in alerte.message


def test_a_drop_without_crossing_the_threshold_raises_a_drift_alert():
    """Le signal utile : quand l'ecart legal se declenche, il est deja tard."""
    alerte = monitoring._alerter("localisation", 0.85, 0.95)

    assert alerte.niveau == "derive"
    assert alerte.delta == -0.10
    assert "coute le moins cher" in alerte.message


def test_a_small_drop_is_treated_as_measurement_noise():
    """Le jeu annote est petit : un ratio se deplace par paliers."""
    assert monitoring._alerter("localisation", 0.98, 1.00) is None


def test_a_rise_is_never_an_alert():
    assert monitoring._alerter("localisation", 0.95, 0.81) is None


def test_without_a_previous_reading_only_the_legal_threshold_applies():
    assert monitoring._alerter("localisation", 0.95, None) is None
    assert monitoring._alerter("localisation", 0.62, None).niveau == "ecart_legal"


def test_the_legal_gap_is_read_before_the_drift(db):
    _releve({"localisation": 0.99, "prenom_et_nom": 0.70})
    controle = monitoring.check(record=False)

    if len(controle.alertes) > 1:
        assert controle.alertes[0].niveau == "ecart_legal"


# --- Historique --------------------------------------------------------------
def test_the_first_check_declares_itself_as_such(db):
    controle = monitoring.check(record=False)

    assert controle.premier_releve
    assert controle.precedents == {}
    assert controle.alertes == [] or all(
        item.precedent is None for item in controle.alertes
    )


def test_a_check_records_its_readings(db):
    monitoring.check()

    entree = AuditLog.objects.get(action=AuditLog.Action.BIAS_MONITORED)
    assert "localisation" in entree.metadata["ratios"]
    assert entree.metadata["engine_version"] == engine.ENGINE_VERSION


def test_a_dry_run_records_nothing(db):
    """Un controle qui ne s'enregistre pas ne repond pas a « depuis quand ? »."""
    monitoring.check(record=False)
    assert not AuditLog.objects.filter(action=AuditLog.Action.BIAS_MONITORED).exists()


def test_the_next_check_compares_to_the_last_reading(db):
    _releve({"localisation": 0.95, "prenom_et_nom": 1.0})
    controle = monitoring.check(record=False)

    assert not controle.premier_releve
    assert controle.precedents["localisation"] == 0.95


def test_a_drop_since_the_last_reading_is_reported(db):
    """Le ratio mesure sur le jeu annote vaut 0.809 ; en partant de 0.95, la
    baisse depasse le seuil de derive."""
    _releve({"localisation": 0.95})
    controle = monitoring.check(record=False)

    derives = [item for item in controle.alertes if item.niveau == "derive"]
    assert any(item.dimension == "localisation" for item in derives)


def test_the_history_reads_from_the_audit_log(db):
    """Le journal est la source : une seconde table divergerait."""
    _releve({"localisation": 0.90})
    _releve({"localisation": 0.85})

    historique = monitoring.historique(dimension="localisation")
    assert [item.ratio for item in historique] == [0.85, 0.90]


def test_the_history_can_be_filtered_by_dimension(db):
    _releve({"localisation": 0.90, "prenom_et_nom": 1.0})

    assert len(monitoring.historique(dimension="localisation")) == 1
    assert len(monitoring.historique()) == 2


def test_the_history_is_bounded(db):
    for index in range(5):
        _releve({"localisation": 0.90 - index / 100})

    historique = monitoring.historique(limit=3)
    assert len(historique) == 3


def test_an_empty_history_is_not_an_error(db):
    assert monitoring.historique() == []
    assert monitoring.derniers_releves() == {}


# --- Le module ne bloque rien ------------------------------------------------
def test_a_check_never_changes_a_score(db):
    """Constater, dater, alerter. Corriger reste une decision humaine."""
    from apps.matching.models import MatchScore

    avant = MatchScore.objects.count()
    monitoring.check()
    assert MatchScore.objects.count() == avant


def test_the_check_serialises(db):
    donnees = monitoring.check(record=False).as_dict()

    assert set(donnees) >= {"conforme", "stable", "releves", "alertes", "worst_ratio"}
    assert "localisation" in donnees["releves"]


# --- Commande ----------------------------------------------------------------
def test_the_command_records_a_reading(db):
    from django.core.management import call_command

    call_command("monitor_bias", verbosity=0)
    assert AuditLog.objects.filter(action=AuditLog.Action.BIAS_MONITORED).count() == 1


def test_the_command_can_measure_without_recording(db):
    from django.core.management import call_command

    call_command("monitor_bias", "--dry-run", verbosity=0)
    assert not AuditLog.objects.filter(action=AuditLog.Action.BIAS_MONITORED).exists()


def test_strict_mode_fails_on_an_alert(db):
    """Utile dans une chaine d'integration, ou une derive doit arreter le train."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    _releve({"localisation": 0.99})
    with pytest.raises(CommandError, match="alerte"):
        call_command("monitor_bias", "--strict", verbosity=0)


def test_without_strict_an_alert_does_not_fail(db):
    from django.core.management import call_command

    _releve({"localisation": 0.99})
    call_command("monitor_bias", verbosity=0)  # ne leve pas


# --- Page --------------------------------------------------------------------
def test_the_transparency_page_shows_the_monitoring(client, db, recruteur):
    client.force_login(recruteur)
    reponse = client.get(reverse("evaluation:bias_report"))

    assert reponse.status_code == 200
    assert reponse.context["monitoring"].releves
    assert "Surveillance continue" in reponse.content.decode()


def test_the_page_records_nothing(client, db, recruteur):
    """Un releve a chaque rafraichissement rendrait l'historique illisible."""
    client.force_login(recruteur)
    client.get(reverse("evaluation:bias_report"))
    client.get(reverse("evaluation:bias_report"))

    assert not AuditLog.objects.filter(action=AuditLog.Action.BIAS_MONITORED).exists()


def test_the_page_shows_the_history(client, db, recruteur):
    _releve({"localisation": 0.90})

    client.force_login(recruteur)
    reponse = client.get(reverse("evaluation:bias_report"))

    assert reponse.context["monitoring_history"]
    assert "0,900" in reponse.content.decode() or "0.900" in reponse.content.decode()


def test_the_page_distinguishes_the_two_levels(client, db, recruteur):
    client.force_login(recruteur)
    contenu = client.get(reverse("evaluation:bias_report")).content.decode()

    assert "ecart legal" in contenu
    assert "derive" in contenu
    assert f"{bias.IMPACT_RATIO_THRESHOLD:.2f}".replace(".", ",") in contenu
