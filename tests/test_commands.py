"""Les commandes de gestion tournent-elles ?

Le README les presente comme le moyen de reproduire chaque mesure du projet :
« lancer `python manage.py evaluate` », « verifier par `purge_expired
--dry-run` ». Aucune n'etait exercee par la suite — la mesure de couverture les
donnait toutes a 0 %, soit pres de la moitie du code non couvert.

Une commande cassee casse donc les instructions du README **en silence**, et le
premier a s'en apercevoir est celui qui clone le depot.

Ces tests sont des tests de fumee : ils verifient qu'une commande s'execute sur
des donnees reelles et produit ce qu'elle annonce. Ils ne remplacent pas les
tests des modules appeles, qui existent par ailleurs — ils garantissent que le
chemin d'entree tient.

Les commandes lourdes (`evaluate`, `audit_bias`, `evaluate_extraction`,
`evaluate_search`) restent hors de ce fichier : elles rejouent le jeu annote
entier, et la CI les lance deja a chaque poussee. Les doubler ici ajouterait
plusieurs minutes a chaque execution locale pour un gain nul.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching.models import MatchScore


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture(autouse=True)
def sans_modele(monkeypatch):
    """Aucun appel au serveur d'inference : ces tests portent sur les commandes."""
    from apps.matching import explain

    monkeypatch.setattr(explain, "explain", lambda *a, **k: {})


@pytest.fixture
def offre(db):
    offre = JobOffer.objects.create(
        title="Backend", description="x", status="open", slug="backend"
    )
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    return offre


@pytest.fixture
def candidature(offre):
    candidat = Candidate.objects.create(
        full_name="Alice Martin", total_experience_years=5
    )
    CandidateSkill.objects.create(
        candidate=candidat, name="Python", years=5, last_used_year=2026
    )
    return Application.objects.create(candidate=candidat, offer=offre)


# --- Le jeu de demonstration --------------------------------------------------
def test_seed_demo_creates_a_usable_application(db, capsys):
    """La premiere commande que lance quelqu'un qui clone le depot. Cassee, elle
    donne une application vide et une mauvaise premiere impression."""
    call_command("seed_demo")

    assert JobOffer.objects.exists()
    assert Candidate.objects.exists()
    assert Application.objects.exists()
    assert "offres" in capsys.readouterr().out


def test_seed_demo_can_be_run_twice(db):
    """Elle est lancee au deploiement, a chaque construction : elle doit etre
    idempotente, sinon chaque mise en ligne duplique le jeu."""
    call_command("seed_demo")
    offres, candidats = JobOffer.objects.count(), Candidate.objects.count()

    call_command("seed_demo")

    assert JobOffer.objects.count() == offres
    assert Candidate.objects.count() == candidats


# --- Le scoring ---------------------------------------------------------------
def test_score_all_scores_the_pending_applications(candidature, capsys):
    call_command("score_all")

    assert MatchScore.objects.filter(application=candidature).exists()
    assert "1" in capsys.readouterr().out


def test_score_all_quiet_keeps_only_the_total(candidature, capsys):
    """`--quiet` supprime le detail par offre, pas le total : au deploiement on
    veut savoir combien de candidatures ont ete scorees, pas lesquelles."""
    call_command("score_all")
    bavard = capsys.readouterr().out

    call_command("score_all", "--quiet")
    discret = capsys.readouterr().out

    assert MatchScore.objects.filter(application=candidature).exists()
    assert "candidature(s) scoree(s)" in discret, "le total reste"
    assert "Backend" in bavard and "Backend" not in discret, "le detail disparait"


def test_score_offer_targets_one_offer(candidature, capsys):
    autre = JobOffer.objects.create(
        title="Data", description="x", status="open", slug="data"
    )
    JobSkill.objects.create(offer=autre, name="SQL", requirement="required")
    ignoree = Application.objects.create(
        candidate=Candidate.objects.create(full_name="Bob"), offer=autre
    )

    call_command("score_offer", "backend", "--no-explain")

    assert MatchScore.objects.filter(application=candidature).exists()
    assert not MatchScore.objects.filter(application=ignoree).exists()


def test_score_offer_refuses_an_unknown_slug(db):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("score_offer", "offre-inexistante")


def test_the_scoring_tasks_run(candidature):
    """Sans broker, Celery s'execute en synchrone : la tache est du code
    ordinaire, et rien ne l'exercait."""
    from apps.matching import tasks

    tasks.score_application_task(str(candidature.pk), with_explanation=False)
    assert MatchScore.objects.filter(application=candidature).exists()

    compte = tasks.score_offer_task(str(candidature.offer.pk), with_explanation=False)
    assert compte >= 1


# --- La purge RGPD ------------------------------------------------------------
def test_purge_dry_run_deletes_nothing(candidature, capsys):
    """Une commande de purge doit pouvoir etre lancee sans rien detruire : c'est
    la seule facon de verifier ce qu'elle ferait."""
    Candidate.objects.filter(pk=candidature.candidate.pk).update(
        retention_until=timezone.now().date() - dt.timedelta(days=1)
    )

    call_command("purge_expired", "--dry-run")

    assert Candidate.objects.filter(pk=candidature.candidate.pk).exists()
    sortie = capsys.readouterr().out
    assert "Simulation" in sortie, "la commande doit dire qu'elle n'a rien supprime"
    assert "Alice Martin" in sortie, "et nommer ce qu'elle aurait purge"


def test_purge_removes_an_expired_file(candidature):
    Candidate.objects.filter(pk=candidature.candidate.pk).update(
        retention_until=timezone.now().date() - dt.timedelta(days=1)
    )

    call_command("purge_expired")

    assert not Candidate.objects.filter(pk=candidature.candidate.pk).exists()


def test_purge_spares_a_file_still_within_its_term(candidature):
    Candidate.objects.filter(pk=candidature.candidate.pk).update(
        retention_until=timezone.now().date() + dt.timedelta(days=30)
    )

    call_command("purge_expired")

    assert Candidate.objects.filter(pk=candidature.candidate.pk).exists()


# --- La sonde du serveur d'inference ------------------------------------------
def test_check_ai_reports_an_unreachable_server(db, settings, capsys):
    """Le cas le plus frequent sur ce projet : le serveur d'inference tourne sur
    un cluster prive et ne repond pas depuis l'exterieur. La commande doit le
    dire, pas lever une trace."""
    settings.LLM = {**settings.LLM, "BASE_URL": "http://127.0.0.1:1/v1"}
    settings.VLM = {**settings.VLM, "BASE_URL": "http://127.0.0.1:1/v1"}

    # La commande signale l'echec par son code de sortie ; c'est sa sortie
    # ecrite qu'on eprouve ici.
    with contextlib.suppress(SystemExit):
        call_command("check_ai", "--skip-embeddings")

    sortie = capsys.readouterr().out
    assert sortie.strip(), "la sonde doit dire ce qu'elle a trouve"


def test_check_ai_names_the_configured_endpoints(db, settings, capsys):
    settings.LLM = {**settings.LLM, "BASE_URL": "http://127.0.0.1:1/v1"}
    settings.VLM = {**settings.VLM, "BASE_URL": "http://127.0.0.1:1/v1"}

    with contextlib.suppress(SystemExit):
        call_command("check_ai", "--skip-embeddings")

    assert "127.0.0.1" in capsys.readouterr().out
