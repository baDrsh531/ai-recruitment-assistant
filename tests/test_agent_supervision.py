"""Trois mesures qui rendent l'agent exploitable, plutot que seulement correct.

`test_agent.py` verifie que l'agent ne peut pas decider. Ces tests-ci portent
sur ce qui vient apres : savoir si la supervision humaine est reelle, surveiller
le systeme entre deux audits, et retrouver les dossiers laisses a moitie.

Le premier bloc est le plus important. « L'agent ne decide pas » se demontre en
lisant le code ; « les recruteurs lisent vraiment » ne se demontre pas du tout,
il se mesure. Un taux de contradiction nul decrit un tampon, et un tampon rend
la garantie structurelle purement formelle.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.agent import adoption, pipeline, watch
from apps.agent.models import Recommendation
from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.core.models import AuditLog
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture(autouse=True)
def agent_actif(settings):
    settings.AGENT_ENABLED = True
    settings.AGENT_DAILY_TOKEN_BUDGET = 0
    return settings


@pytest.fixture
def offre(db):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    JobSkill.objects.create(offer=offre, name="Django", requirement="required")
    return offre


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


def _candidature(offre, nom, competences=("Python", "Django"), annees=5):
    candidat = Candidate.objects.create(full_name=nom, total_experience_years=annees)
    for competence in competences:
        CandidateSkill.objects.create(
            candidate=candidat, name=competence, years=annees, last_used_year=2026
        )
    return Application.objects.create(candidate=candidat, offer=offre)


def _reco(offre, index, *, statut, etape=Application.Stage.REJECTED, resolue=None):
    candidature = _candidature(offre, f"Candidat {index}")
    return Recommendation.objects.create(
        application=candidature,
        proposed_stage=etape,
        rationale="motif",
        score_at_time=0.5,
        threshold_at_time=0.85,
        status=statut,
        resolved_at=resolue,
    )


# --- La supervision est-elle reelle ? ----------------------------------------
def test_nothing_can_be_concluded_before_a_single_decision(db):
    mesure = adoption.mesurer()

    assert mesure.global_.tranchees == 0
    assert not mesure.assez_de_recul
    assert "aucune proposition" in mesure.lecture.lower()


def test_an_agent_never_contradicted_is_flagged_as_a_possible_rubber_stamp(db, offre):
    """Le point de tout le module : 100 % de suivi est un signal, pas un succes."""
    for index in range(40):
        _reco(offre, index, statut=Recommendation.Status.ACCEPTED)

    mesure = adoption.mesurer()

    assert mesure.global_.pourcentage == 0
    assert mesure.tampon_possible
    assert "valide sans etre lu" in mesure.lecture


def test_the_rubber_stamp_alert_needs_thirty_five_decisions_to_fire(db, offre):
    """Le prix a payer pour que l'alerte veuille dire quelque chose.

    Elle se declenche sur la borne haute de l'intervalle, pas sur le taux :
    sans aucune contradiction cette borne vaut z²/(n+z²), soit 10 % a partir de
    35 decisions. Trois suivis sur trois ne prouvent rien, et une alerte qui
    tomberait la serait du bruit.
    """
    for index in range(34):
        _reco(offre, index, statut=Recommendation.Status.ACCEPTED)
    assert not adoption.mesurer().tampon_possible

    _reco(offre, 34, statut=Recommendation.Status.ACCEPTED)
    assert adoption.mesurer().tampon_possible


def test_a_rate_measured_on_four_decisions_is_declared_unreadable(db, offre):
    """25 % sur quatre decisions est compatible avec a peu pres tout."""
    _reco(offre, 0, statut=Recommendation.Status.REJECTED)
    for index in range(1, 4):
        _reco(offre, index, statut=Recommendation.Status.ACCEPTED)

    mesure = adoption.mesurer()

    assert mesure.global_.pourcentage == 25
    assert not mesure.assez_de_recul
    # Valeurs citees dans le README et dans le module. Les figer ici evite que
    # la prose et le calcul divergent sans que rien ne le signale.
    assert (mesure.global_.borne_basse, mesure.global_.borne_haute) == (5, 70)
    assert "trop large pour conclure" in mesure.lecture.lower()


def test_a_healthy_rate_on_enough_decisions_becomes_readable(db, offre):
    for index in range(30):
        statut = (
            Recommendation.Status.REJECTED
            if index % 4 == 0
            else Recommendation.Status.ACCEPTED
        )
        _reco(offre, index, statut=statut)

    mesure = adoption.mesurer()

    assert mesure.assez_de_recul
    assert not mesure.tampon_possible
    assert "lisent les dossiers" in mesure.lecture


def test_the_confidence_interval_never_leaves_zero_hundred(db, offre):
    """L'approximation normale sortirait des bornes sur ces effectifs.

    C'est la raison du choix de Wilson : un intervalle affiche a -12 % se voit
    et decredibilise le reste de la page.
    """
    for total in (1, 2, 3, 5):
        Recommendation.objects.all().delete()
        for index in range(total):
            _reco(offre, index, statut=Recommendation.Status.ACCEPTED)

        taux = adoption.mesurer().global_

        assert 0 <= taux.borne_basse <= taux.borne_haute <= 100


@pytest.mark.parametrize("contredites,tranchees", [
    (0, 1), (1, 1), (0, 7), (7, 7), (3, 7), (1, 100), (99, 100), (50, 100),
])
def test_the_drawn_band_never_leaves_the_frame(contredites, tranchees):
    """Le trace est pose en pourcentages : `left` + `width` doit rester dans le
    cadre, sur toute la plage, extremites comprises."""
    taux = adoption.Taux(
        libelle="x", contredites=contredites, tranchees=tranchees
    )

    assert 0 <= taux.borne_basse <= 100
    assert 0 <= taux.borne_haute <= 100
    assert taux.largeur >= 0
    assert taux.borne_basse + taux.largeur <= 100
    assert taux.borne_basse <= taux.pourcentage <= taux.borne_haute


def test_the_breakdown_separates_proposed_rejections_from_proposed_interviews(
    db, offre
):
    """Contredire les rejets et valider les entretiens sans broncher decrit une
    supervision qui se relache la ou elle engage le moins — un resultat que le
    seul taux global cache."""
    for index in range(10):
        _reco(
            offre, index, statut=Recommendation.Status.REJECTED,
            etape=Application.Stage.REJECTED,
        )
    for index in range(10, 20):
        _reco(
            offre, index, statut=Recommendation.Status.ACCEPTED,
            etape=Application.Stage.SCREENING,
        )

    par_type = {item.libelle: item for item in adoption.mesurer().par_type}

    assert par_type["Rejets proposes"].pourcentage == 100
    assert par_type["Mises en entretien proposees"].pourcentage == 0


def test_the_breakdown_always_adds_up_to_the_total(db, offre):
    """Une etape inconnue du dictionnaire de libelles ne doit pas disparaitre
    de la ventilation tout en comptant dans le total."""
    for index in range(6):
        _reco(
            offre, index, statut=Recommendation.Status.ACCEPTED,
            etape=Application.Stage.REJECTED,
        )
    for index in range(6, 10):
        _reco(
            offre, index, statut=Recommendation.Status.REJECTED,
            etape=Application.Stage.TECHNICAL,  # hors de LIBELLES
        )

    mesure = adoption.mesurer()

    assert sum(item.tranchees for item in mesure.par_type) == mesure.global_.tranchees
    assert sum(item.contredites for item in mesure.par_type) == mesure.global_.contredites
    assert any("technical" in item.libelle for item in mesure.par_type)


def test_pending_and_stale_recommendations_are_not_counted_as_decisions(db, offre):
    """Une proposition en attente n'est ni suivie ni contredite."""
    _reco(offre, 0, statut=Recommendation.Status.PENDING)
    _reco(offre, 1, statut=Recommendation.Status.STALE)
    _reco(offre, 2, statut=Recommendation.Status.ACCEPTED)

    mesure = adoption.mesurer()

    assert mesure.global_.tranchees == 1
    assert mesure.en_attente == 1
    assert mesure.perimees == 1
    assert mesure.total == 3


def test_the_rate_can_be_narrowed_to_one_offer(db, offre):
    """Un taux global peut cacher un service qui valide tout."""
    autre = JobOffer.objects.create(title="Data", description="x", status="open")
    JobSkill.objects.create(offer=autre, name="Python", requirement="required")

    for index in range(5):
        _reco(offre, index, statut=Recommendation.Status.ACCEPTED)
    for index in range(5, 10):
        reco = _reco(autre, index, statut=Recommendation.Status.REJECTED)
        assert reco.application.offer == autre

    assert adoption.mesurer(offer=offre).global_.pourcentage == 0
    assert adoption.mesurer(offer=autre).global_.pourcentage == 100
    assert adoption.mesurer().global_.pourcentage == 50


def test_the_median_delay_is_measured_on_resolved_recommendations_only(db, offre):
    maintenant = timezone.now()
    for index, minutes in enumerate((10, 20, 30)):
        reco = _reco(offre, index, statut=Recommendation.Status.ACCEPTED)
        Recommendation.objects.filter(pk=reco.pk).update(
            created_at=maintenant, resolved_at=maintenant + dt.timedelta(minutes=minutes)
        )
    _reco(offre, 9, statut=Recommendation.Status.PENDING)

    assert adoption.mesurer().delai_median_min == pytest.approx(20, abs=0.2)


def test_a_real_resolution_feeds_the_measure(db, offre, recruteur):
    """Le compteur doit suivre le vrai chemin, pas seulement des lignes ecrites
    a la main dans le test."""
    candidature = _candidature(offre, "Alice")
    reco = Recommendation.objects.create(
        application=candidature,
        proposed_stage=Application.Stage.REJECTED,
        rationale="motif",
        score_at_time=0.4,
        threshold_at_time=0.85,
    )

    pipeline.resoudre(reco, accepter=False, actor=recruteur, note="je ne suis pas")

    mesure = adoption.mesurer()
    assert mesure.global_.tranchees == 1
    assert mesure.global_.contredites == 1
    assert mesure.delai_median_min is not None


# --- La veille ---------------------------------------------------------------
def test_the_watch_runs_while_the_agent_is_switched_off(db, settings):
    """L'interrupteur protege la depense, pas la surveillance.

    Couper l'agent pour economiser et perdre au passage le controle de biais
    serait un mauvais echange — d'autant que cette tache ne coute rien.
    """
    settings.AGENT_ENABLED = False

    controle = watch.veiller()

    assert controle.releves
    assert watch.dernier_controle() is not None


def test_the_watch_spends_no_token(db):
    """Le ratio d'impact se calcule par le moteur deterministe.

    Si un appel au modele s'y glissait, la veille deviendrait soumise au
    plafond et s'arreterait en meme temps que ce qu'elle surveille.
    """
    from apps.ai.models import AIInvocation

    watch.veiller()

    assert not AIInvocation.objects.exists()


def test_the_watch_is_attributed_to_the_agent_in_the_journal(db):
    watch.veiller()

    entree = AuditLog.objects.filter(action=AuditLog.Action.AGENT_WATCHED).first()
    assert entree is not None
    assert entree.metadata["agent"] is True
    assert entree.actor.is_agent


def test_a_recorded_watch_answers_since_when(db):
    """Un ratio stable depuis six mois et un ratio non mesure depuis six mois
    se ressemblent dans un tableau ; ils n'ont pas la meme valeur."""
    assert watch.dernier_controle() is None

    watch.veiller()
    watch.veiller()

    assert len(watch.historique()) == 2
    assert watch.dernier_controle() is not None


def test_alerts_of_the_last_watch_are_readable_without_recomputing(db, monkeypatch):
    from apps.evaluation import monitoring

    def _controle_avec_alerte(**kwargs):
        resultat = monitoring.Controle(date=timezone.now())
        resultat.alertes.append(
            monitoring.Alerte(
                niveau="ecart_legal",
                dimension="genre",
                ratio=0.62,
                precedent=0.91,
                message="ecart constate",
            )
        )
        return resultat

    monkeypatch.setattr(monitoring, "check", _controle_avec_alerte)

    watch.veiller()

    alertes = watch.alertes_en_cours()
    assert len(alertes) == 1
    assert alertes[0]["niveau"] == "ecart_legal"
    assert alertes[0]["delta"] == pytest.approx(-0.29, abs=0.001)


def test_the_watch_command_runs(db):
    call_command("agent_watch")


def test_the_periodic_watch_task_is_scheduled_and_runs(db, settings):
    """Un controle qui depend d'une page qu'on doit penser a ouvrir ne se
    declenche jamais entre deux audits."""
    from apps.agent import tasks

    programme = settings.CELERY_BEAT_SCHEDULE["veille-derive-biais"]
    assert programme["task"] == "apps.agent.tasks.watch_task"

    resultat = tasks.watch_task()

    assert resultat["alertes"] == 0
    assert watch.dernier_controle() is not None


def test_the_periodic_watch_ignores_the_agent_switch(db, settings):
    settings.AGENT_ENABLED = False
    from apps.agent import tasks

    assert tasks.watch_task()["conforme"] is True


# --- La file de reprise ------------------------------------------------------
def test_a_file_left_half_prepared_is_listed(db, offre, monkeypatch):
    """C'est ce dossier-la qui trompe : il a un score, il s'affiche comme les
    autres, et il lui manque l'analyse sur laquelle un recruteur croit
    s'appuyer."""
    monkeypatch.setattr(pipeline, "_rediger", lambda application, run: None)
    monkeypatch.setattr(pipeline, "_questionner", lambda application, run: None)
    candidature = _candidature(offre, "Alice")

    pipeline.run()

    dossiers = {item.application.pk: item for item in pipeline.incomplets()}
    assert candidature.pk in dossiers
    dossier = dossiers[candidature.pk]
    assert "score" in dossier.faites
    assert "analyse" in dossier.manquantes
    assert not dossier.jamais_touche


def test_a_never_processed_file_is_not_confused_with_a_failure(db, offre):
    candidature = _candidature(offre, "Alice")

    dossiers = {item.application.pk: item for item in pipeline.incomplets()}

    assert dossiers[candidature.pk].jamais_touche
    assert dossiers[candidature.pk].faites == []


def test_missing_only_model_steps_points_at_the_server_not_at_a_defect(
    db, offre, monkeypatch
):
    """La distinction qui rend la liste exploitable.

    Il manque le score : c'est un defaut, ce calcul est local et deterministe.
    Il ne manque que l'analyse : c'est le serveur, il n'y a rien a corriger.
    """
    monkeypatch.setattr(pipeline, "_rediger", lambda application, run: None)
    monkeypatch.setattr(pipeline, "_questionner", lambda application, run: None)
    _candidature(offre, "Alice")

    pipeline.run()

    dossier = next(item for item in pipeline.incomplets() if not item.jamais_touche)
    assert dossier.bloque_sur_le_modele


def test_a_complete_file_disappears_from_the_queue(db, offre, monkeypatch):
    monkeypatch.setattr(pipeline, "_rediger", lambda application, run: None)
    monkeypatch.setattr(pipeline, "_analyse_faite", lambda application: True)
    monkeypatch.setattr(pipeline, "_questionner", lambda application, run: None)
    monkeypatch.setattr(pipeline, "_questions_faites", lambda application: True)
    candidature = _candidature(offre, "Alice")

    pipeline.run()

    restants = [item.application.pk for item in pipeline.incomplets()]
    assert candidature.pk not in restants


def test_half_prepared_files_come_before_untouched_ones(db, offre, monkeypatch):
    """Un dossier jamais touche attend son tour ; un dossier a moitie prepare
    est un probleme."""
    monkeypatch.setattr(pipeline, "_rediger", lambda application, run: None)
    monkeypatch.setattr(pipeline, "_questionner", lambda application, run: None)
    entame = _candidature(offre, "Entame")
    pipeline.run(applications=[entame])
    _candidature(offre, "Jamais touche")

    dossiers = pipeline.incomplets()

    assert dossiers[0].application.pk == entame.pk
    assert dossiers[-1].jamais_touche


def test_a_decided_file_is_never_in_the_queue(db, offre):
    """Regenerer des questions sur un dossier clos couterait des tokens pour
    rien."""
    candidature = _candidature(offre, "Alice")
    Application.objects.filter(pk=candidature.pk).update(
        stage=Application.Stage.REJECTED
    )

    assert pipeline.incomplets() == []


# --- Ce que la page montre ---------------------------------------------------
def test_the_dashboard_shows_the_three_measures(db, client, recruteur):
    client.force_login(recruteur)

    reponse = client.get("/agent/")

    assert reponse.status_code == 200
    contenu = reponse.content.decode()
    assert "supervision est-elle reelle" in contenu
    assert "Veille sur la derive" in contenu
    assert "Dossiers restes incomplets" in contenu


def test_the_dashboard_renders_the_interval_once_there_are_decisions(
    db, client, recruteur, offre, monkeypatch
):
    """Une page vide ne prouve pas qu'elle sait afficher des chiffres.

    Le trace de l'intervalle et le tableau par type ne sortent que lorsqu'il y
    a des donnees ; les tester a vide laisserait passer une erreur de gabarit.
    """
    monkeypatch.setattr(pipeline, "_rediger", lambda application, run: None)
    monkeypatch.setattr(pipeline, "_questionner", lambda application, run: None)
    for index in range(12):
        _reco(
            offre, index, statut=Recommendation.Status.ACCEPTED,
            etape=Application.Stage.SCREENING,
        )
    for index in range(12, 20):
        _reco(
            offre, index, statut=Recommendation.Status.REJECTED,
            etape=Application.Stage.REJECTED,
        )
    pipeline.run()
    client.force_login(recruteur)

    contenu = client.get("/agent/").content.decode()

    assert "range__band" in contenu
    assert "Mises en entretien proposees" in contenu
    assert "Rejets proposes" in contenu
    # Les dossiers a moitie prepares doivent apparaitre, pas seulement compter.
    assert "serveur d&#x27;inference" in contenu or "serveur d'inference" in contenu


def test_the_watch_can_be_relaunched_from_the_page(db, client, recruteur):
    client.force_login(recruteur)

    reponse = client.post("/agent/veille/")

    assert reponse.status_code == 302
    assert watch.dernier_controle() is not None
