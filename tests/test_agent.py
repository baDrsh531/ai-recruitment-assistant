"""Tests de l'agent d'orchestration.

L'agent n'apporte aucune capacite nouvelle : lire un CV, scorer, generer des
questions existaient deja et sont testes ailleurs. Ces tests portent donc sur
ce qui est propre a l'agent, et surtout sur ses **limites** — c'est la que se
joue la these du projet. Un agent qui pourrait trancher detruirait l'argument
que tout le reste construit.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.agent import budget, pipeline
from apps.agent.models import AgentRun, Recommendation
from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.core.models import AuditLog
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching.services import DecisionRefused, score_application


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture(autouse=True)
def agent_actif(settings):
    settings.AGENT_ENABLED = True
    settings.AGENT_DAILY_TOKEN_BUDGET = 0  # illimite par defaut dans les tests
    return settings


@pytest.fixture(autouse=True)
def sans_modele(monkeypatch):
    """L'analyse redigee et les questions demandent un serveur d'inference.

    Les tests portent sur l'orchestration, pas sur le modele : les deux etapes
    qui l'appellent sont neutralisees, et un test dedie verifie que leur echec
    n'arrete pas l'agent.
    """
    monkeypatch.setattr(pipeline, "_rediger", lambda application, run: None)
    monkeypatch.setattr(pipeline, "_analyse_faite", lambda application: True)
    monkeypatch.setattr(pipeline, "_questionner", lambda application, run: None)
    monkeypatch.setattr(pipeline, "_questions_faites", lambda application: True)


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


@pytest.fixture
def offre(db):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    JobSkill.objects.create(offer=offre, name="Django", requirement="required")
    return offre


def _candidature(offre, nom, competences, annees=5):
    candidat = Candidate.objects.create(full_name=nom, total_experience_years=annees)
    for competence in competences:
        CandidateSkill.objects.create(
            candidate=candidat, name=competence, years=annees, last_used_year=2026
        )
    return Application.objects.create(candidate=candidat, offer=offre)


# --- La garantie structurelle ------------------------------------------------
def test_the_agent_account_cannot_decide(db):
    """La garantie ne tient pas a une consigne : elle tient au role.

    Meme un appel direct a `decide()` avec le compte de l'agent est refuse,
    parce que son role est hors de `can_decide`.
    """
    compte = pipeline.compte_agent()

    assert compte.is_agent
    assert not compte.can_decide


def test_the_agent_account_cannot_log_in(db):
    """Un compte non humain n'a pas a pouvoir se connecter."""
    compte = pipeline.compte_agent()

    assert not compte.is_active
    assert not compte.has_usable_password()


def test_a_direct_decide_call_by_the_agent_is_refused(db, offre):
    from apps.matching.services import decide

    candidature = _candidature(offre, "Alice", ["Python", "Django"])

    with pytest.raises(DecisionRefused, match="habilite"):
        decide(
            candidature, stage="rejected", note="x" * 20, actor=pipeline.compte_agent()
        )


def test_the_agent_never_moves_an_application(db, offre):
    """Le test qui compte : preparer, oui ; faire avancer, jamais."""
    candidature = _candidature(offre, "Alice", ["Python", "Django"])

    pipeline.run()
    candidature.refresh_from_db()

    assert candidature.stage == Application.Stage.RECEIVED
    assert candidature.decided_by is None
    assert candidature.decided_at is None


# --- Ce que l'agent fait -----------------------------------------------------
def test_the_agent_scores_a_pending_application(db, offre):
    candidature = _candidature(offre, "Alice", ["Python", "Django"])

    resultat = pipeline.run()

    assert candidature.scores.exists()
    assert resultat.run.applications_processed == 1
    assert resultat.run.status == AgentRun.Status.DONE


def test_the_agent_writes_a_recommendation(db, offre):
    candidature = _candidature(offre, "Alice", ["Python", "Django"])

    pipeline.run()
    recommandation = candidature.recommendations.get()

    assert recommandation.pending
    assert recommandation.proposed_stage == Application.Stage.SCREENING
    assert "seuil" in recommandation.rationale


def test_a_weak_profile_gets_a_rejection_proposal_with_its_caveat(db, offre):
    candidature = _candidature(offre, "Faible", [], annees=0)

    pipeline.run()
    recommandation = candidature.recommendations.get()

    assert recommandation.proposed_stage == Application.Stage.REJECTED
    assert recommandation.proposes_rejection
    # Une proposition de rejet doit porter sa propre reserve.
    assert "pas sur la personne" in recommandation.rationale


def test_no_recommendation_in_the_borderline_band(db, offre, monkeypatch):
    """Sur un dossier limite, une proposition ecrite peserait plus que le
    chiffre ne le justifie."""
    candidature = _candidature(offre, "Limite", ["Python", "Django"])
    score = score_application(candidature, with_explanation=False)

    from apps.evaluation import threshold as calibration

    monkeypatch.setattr(
        calibration, "recommended_threshold", lambda *a, **k: score.effective_score - 0.01
    )
    pipeline.run()

    assert not candidature.recommendations.exists()


def test_a_decided_application_is_left_alone(db, offre):
    """Preparer un entretien pour quelqu'un qu'on a ecarte n'a pas de sens."""
    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    Application.objects.filter(pk=candidature.pk).update(stage="rejected")

    resultat = pipeline.run()

    assert resultat.run.applications_seen == 0
    assert not candidature.recommendations.exists()


def test_no_interview_questions_for_a_profile_proposed_for_rejection(
    db, offre, monkeypatch
):
    """Preparer un entretien pour quelqu'un qu'on propose d'ecarter depense des
    tokens pour un entretien qui n'aura pas lieu — la moitie du cout d'un
    dossier sur le jeu de demonstration."""
    appels = []
    # La fixture globale neutralise `_questions_faites` ; on remet la vraie
    # regle, puisque c'est elle qu'on veut eprouver.
    monkeypatch.setattr(pipeline, "_questions_faites", _regle_questions)
    monkeypatch.setattr(
        pipeline, "_questionner", lambda application, run: appels.append(application.pk)
    )
    _candidature(offre, "Faible", [], annees=0)

    pipeline.run()

    assert appels == [], "aucune question ne doit etre generee sur un rejet propose"


def _regle_questions(application):
    """La vraie regle : deja faites, ou inutiles parce qu'un rejet est propose."""
    if application.interview_questions.exists():
        return True
    return application.recommendations.filter(
        status=Recommendation.Status.PENDING,
        proposed_stage__in=[Application.Stage.REJECTED, Application.Stage.WITHDRAWN],
    ).exists()


def test_interview_questions_are_prepared_for_a_favourable_profile(
    db, offre, monkeypatch
):
    appels = []
    monkeypatch.setattr(pipeline, "_questions_faites", _regle_questions)
    monkeypatch.setattr(
        pipeline, "_questionner", lambda application, run: appels.append(application.pk)
    )
    candidature = _candidature(offre, "Alice", ["Python", "Django"])

    pipeline.run()

    assert appels == [candidature.pk]


def test_the_recommendation_comes_before_the_questions():
    """L'ordre porte une decision de cout, pas une preference de style."""
    noms = [etape.nom for etape in pipeline.ETAPES]
    assert noms.index("recommandation") < noms.index("questions")


# --- Reprise -----------------------------------------------------------------
def test_a_second_run_redoes_nothing(db, offre):
    """Le serveur d'inference tombe souvent : reprendre tout couterait le meme
    travail plusieurs fois."""
    _candidature(offre, "Alice", ["Python", "Django"])

    premier = pipeline.run()
    second = pipeline.run()

    assert premier.run.steps_done > 0
    assert second.run.steps_done == 0
    assert second.run.applications_processed == 0


def test_a_score_from_an_older_engine_is_recomputed(db, offre):
    """Un score calcule par une version anterieure n'est pas comparable."""
    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    score = score_application(candidature, with_explanation=False)
    candidature.scores.filter(pk=score.pk).update(engine_version="0.9.0")

    pipeline.run()

    assert candidature.scores.count() == 2


def test_a_failing_step_does_not_stop_the_run(db, offre, monkeypatch):
    from apps.ai.client import InferenceError

    _candidature(offre, "Alice", ["Python", "Django"])

    def tombe(application, run):
        raise InferenceError("serveur injoignable")

    monkeypatch.setattr(pipeline, "_analyse_faite", lambda application: False)
    monkeypatch.setattr(pipeline, "_rediger", tombe)

    resultat = pipeline.run()

    assert resultat.run.steps_failed >= 1
    assert resultat.run.status == AgentRun.Status.DONE
    # Le score, lui, ne depend pas du modele : il a bien ete calcule.
    assert Application.objects.get(candidate__full_name="Alice").scores.exists()


# --- Les freins --------------------------------------------------------------
def test_the_kill_switch_stops_everything(db, offre, settings):
    settings.AGENT_ENABLED = False
    _candidature(offre, "Alice", ["Python", "Django"])

    resultat = pipeline.run()

    assert resultat.desactive
    assert resultat.run.status == AgentRun.Status.DISABLED
    assert not Recommendation.objects.exists()


def test_the_kill_switch_is_read_at_each_run(db, offre, settings):
    """Un interrupteur qui ne prendrait effet qu'au redemarrage arriverait
    trop tard."""
    _candidature(offre, "Alice", ["Python", "Django"])

    settings.AGENT_ENABLED = False
    assert pipeline.run().desactive

    settings.AGENT_ENABLED = True
    assert not pipeline.run().desactive


def test_an_exhausted_budget_stops_the_run(db, offre, settings, monkeypatch):
    settings.AGENT_DAILY_TOKEN_BUDGET = 100
    monkeypatch.setattr(budget, "consommation", lambda depuis=None: 500)
    _candidature(offre, "Alice", ["Python", "Django"])

    resultat = pipeline.run()

    assert resultat.arrete_par_le_budget
    assert resultat.run.status == AgentRun.Status.BUDGET
    assert not Recommendation.objects.exists()


def test_the_budget_counts_input_and_output(db):
    """Un prompt de 4 000 tokens coute meme si la reponse en fait 20."""
    from apps.ai.models import AIInvocation

    AIInvocation.objects.create(
        purpose="test", model="m", status="ok",
        prompt_tokens=4000, completion_tokens=20, latency_ms=10,
    )
    assert budget.consommation() == 4020


def test_an_unlimited_budget_is_never_exhausted(settings, db):
    settings.AGENT_DAILY_TOKEN_BUDGET = 0
    etat = budget.actuel()

    assert etat.illimite
    assert not etat.epuise


# --- La decision reste humaine -----------------------------------------------
def test_accepting_a_recommendation_credits_the_human(db, offre, recruteur):
    """Le point central : celui qui repond de la decision est celui qui a
    clique, pas l'agent qui l'a preparee."""
    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()
    recommandation = candidature.recommendations.get()

    pipeline.resoudre(recommandation, accepter=True, actor=recruteur)

    candidature.refresh_from_db()
    recommandation.refresh_from_db()

    assert candidature.stage == recommandation.proposed_stage
    assert candidature.decided_by == recruteur, "la decision est imputee a l'humain"
    assert recommandation.resolved_by == recruteur
    assert recommandation.status == Recommendation.Status.ACCEPTED

    entree = AuditLog.objects.filter(
        action=AuditLog.Action.STAGE_CHANGED, object_id=str(candidature.pk)
    ).latest("created_at")
    assert entree.actor == recruteur
    assert not entree.actor.is_agent


def test_rejecting_a_recommendation_changes_nothing(db, offre, recruteur):
    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()
    recommandation = candidature.recommendations.get()

    pipeline.resoudre(
        recommandation, accepter=False, actor=recruteur, note="Je le recois quand meme"
    )

    candidature.refresh_from_db()
    recommandation.refresh_from_db()

    assert candidature.stage == Application.Stage.RECEIVED
    assert recommandation.status == Recommendation.Status.REJECTED
    assert recommandation.resolution_note == "Je le recois quand meme"


def test_a_viewer_cannot_resolve_a_recommendation(db, offre, django_user_model):
    observateur = django_user_model.objects.create_user(
        username="obs", password="mot-de-passe-de-test-123", role="viewer"
    )
    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()
    recommandation = candidature.recommendations.get()

    with pytest.raises(DecisionRefused, match="habilite"):
        pipeline.resoudre(recommandation, accepter=True, actor=observateur)


def test_a_recommendation_cannot_be_resolved_twice(db, offre, recruteur):
    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()
    recommandation = candidature.recommendations.get()

    pipeline.resoudre(recommandation, accepter=False, actor=recruteur)
    with pytest.raises(DecisionRefused, match="deja"):
        pipeline.resoudre(recommandation, accepter=True, actor=recruteur)


def test_a_recommendation_goes_stale_when_the_score_changes(db, offre, recruteur):
    """Presenter une proposition calculee sur des chiffres perimes pousserait
    a decider sur autre chose que ce qu'on voit."""
    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()

    candidature.candidate.skills.all().delete()
    score_application(candidature, with_explanation=False)
    perimees = pipeline.perimer_les_recommandations(candidature)

    assert perimees == 1
    assert candidature.recommendations.get().status == Recommendation.Status.STALE


# --- Journal -----------------------------------------------------------------
def test_every_run_is_journalised_as_non_human(db, offre):
    _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()

    entree = AuditLog.objects.get(action=AuditLog.Action.AGENT_RAN)
    assert entree.metadata["agent"] is True
    assert entree.actor.is_agent


def test_a_recommendation_is_journalised(db, offre):
    _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()

    entree = AuditLog.objects.get(action=AuditLog.Action.AGENT_RECOMMENDED)
    assert entree.metadata["agent"] is True
    assert "stage" in entree.metadata


def test_a_run_records_what_it_cost(db, offre):
    _candidature(offre, "Alice", ["Python", "Django"])
    execution = pipeline.run().run

    assert execution.duration_ms >= 0
    assert execution.applications_seen == 1
    assert execution.failed_ratio == 0.0


# --- Declenchement -----------------------------------------------------------
def test_without_a_broker_nothing_is_triggered_inline(db, offre, settings):
    """Celery en synchrone bloquerait le depot du CV pendant que le modele
    redige : mieux vaut differer que figer la page."""
    from apps.agent.tasks import declencher

    settings.CELERY_BROKER_URL = ""
    candidature = _candidature(offre, "Alice", ["Python", "Django"])

    assert declencher(candidature) is False
    assert not candidature.scores.exists()


def test_with_a_broker_the_work_is_handed_over(db, offre, settings, monkeypatch):
    from apps.agent import tasks

    settings.CELERY_BROKER_URL = "redis://localhost:6379/0"
    appels = []
    monkeypatch.setattr(
        tasks.run_agent_task, "delay", lambda *args: appels.append(args)
    )
    candidature = _candidature(offre, "Alice", ["Python", "Django"])

    assert tasks.declencher(candidature) is True
    assert appels == [(str(candidature.pk), "upload")]


def test_a_disabled_agent_is_never_triggered(db, offre, settings):
    from apps.agent.tasks import declencher

    settings.AGENT_ENABLED = False
    settings.CELERY_BROKER_URL = "redis://localhost:6379/0"

    assert declencher(_candidature(offre, "Alice", ["Python"])) is False


# --- Commande ----------------------------------------------------------------
def test_the_command_runs_the_agent(db, offre):
    _candidature(offre, "Alice", ["Python", "Django"])

    call_command("run_agent", verbosity=0)

    assert Recommendation.objects.count() == 1
    assert AgentRun.objects.filter(status=AgentRun.Status.DONE).exists()


def test_the_dry_run_executes_nothing(db, offre):
    _candidature(offre, "Alice", ["Python", "Django"])

    call_command("run_agent", "--dry-run", verbosity=0)

    assert not Recommendation.objects.exists()
    assert not AgentRun.objects.exists()


def test_the_command_respects_the_kill_switch(db, offre, settings):
    settings.AGENT_ENABLED = False
    _candidature(offre, "Alice", ["Python", "Django"])

    call_command("run_agent", verbosity=0)

    assert not Recommendation.objects.exists()


def test_the_limit_is_respected(db, offre):
    for index in range(4):
        _candidature(offre, f"Candidat {index}", ["Python", "Django"])

    call_command("run_agent", "--limit", "2", verbosity=0)

    assert Recommendation.objects.count() == 2


# --- Interface ---------------------------------------------------------------
def test_the_agent_page_renders(client, db, offre, recruteur):
    from django.urls import reverse

    _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()

    client.force_login(recruteur)
    reponse = client.get(reverse("agent:dashboard"))

    assert reponse.status_code == 200
    assert reponse.context["runs"]
    assert reponse.context["pending"]


def test_the_page_states_what_the_agent_does_not_do(client, db, recruteur):
    from django.urls import reverse

    client.force_login(recruteur)
    contenu = client.get(reverse("agent:dashboard")).content.decode()

    assert "Ce que l&#x27;agent ne fait pas" in contenu or "ne fait pas" in contenu
    assert "supervision humaine" in contenu


def test_the_application_page_offers_the_recommendation(client, db, offre, recruteur):
    from django.urls import reverse

    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()

    client.force_login(recruteur)
    reponse = client.get(
        reverse("candidates:application_detail", kwargs={"pk": candidature.pk})
    )

    assert reponse.context["recommendation"] is not None
    contenu = reponse.content.decode()
    assert "Proposition de" in contenu
    assert "Suivre la proposition" in contenu


def test_following_the_recommendation_from_the_page_credits_the_human(
    client, db, offre, recruteur
):
    from django.urls import reverse

    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()
    recommandation = candidature.recommendations.get()

    client.force_login(recruteur)
    client.post(
        reverse("agent:resolve", kwargs={"pk": recommandation.pk}),
        {"action": "accepter", "note": "D'accord avec l'analyse"},
        follow=True,
    )

    candidature.refresh_from_db()
    assert candidature.stage == recommandation.proposed_stage
    assert candidature.decided_by == recruteur
    assert candidature.decision_note == "D'accord avec l'analyse"


def test_a_viewer_is_refused_on_the_resolve_view(client, db, offre, django_user_model):
    from django.urls import reverse

    observateur = django_user_model.objects.create_user(
        username="obs", password="mot-de-passe-de-test-123", role="viewer"
    )
    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()
    recommandation = candidature.recommendations.get()

    client.force_login(observateur)
    reponse = client.post(
        reverse("agent:resolve", kwargs={"pk": recommandation.pk}),
        {"action": "accepter"},
        HTTP_REFERER="/", follow=True,
    )

    messages = [str(m) for m in reponse.context["messages"]]
    assert any("role" in message.lower() for message in messages)
    candidature.refresh_from_db()
    assert candidature.stage == Application.Stage.RECEIVED


def test_a_stale_recommendation_is_not_offered(client, db, offre, recruteur):
    """La page perime d'elle-meme une proposition qui ne porte plus sur le
    dernier score."""
    from django.urls import reverse

    candidature = _candidature(offre, "Alice", ["Python", "Django"])
    pipeline.run()

    candidature.candidate.skills.all().delete()
    score_application(candidature, with_explanation=False)

    client.force_login(recruteur)
    reponse = client.get(
        reverse("candidates:application_detail", kwargs={"pk": candidature.pk})
    )

    assert reponse.context["recommendation"] is None
    assert candidature.recommendations.get().status == Recommendation.Status.STALE
