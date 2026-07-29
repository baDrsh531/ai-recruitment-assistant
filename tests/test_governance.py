"""Tests de gouvernance : decision humaine, roles, conservation des donnees.

Ces trois mecanismes etaient annonces par le projet sans etre appliques : la
fonction `decide()` n'etait appelee par aucune vue, `can_decide` n'etait
verifie nulle part, et la date de conservation n'etait ni ecrite ni respectee.
Les tests ci-dessous existent pour que cela ne redevienne pas le cas.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from apps.candidates import retention
from apps.candidates.models import Application, Candidate, CandidateSkill, CVDocument
from apps.core.models import AuditLog
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching.services import DecisionRefused, decide


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture
def offre(db):
    offer = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offer, name="Python")
    return offer


@pytest.fixture
def candidature(offre):
    candidate = Candidate.objects.create(
        full_name="Alice Martin", email="alice@example.com", total_experience_years=4
    )
    CandidateSkill.objects.create(candidate=candidate, name="Python", years=4)
    return Application.objects.create(candidate=candidate, offer=offre)


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


@pytest.fixture
def observateur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="obs", password="mot-de-passe-de-test-123", role="viewer"
    )


# --- Decision humaine --------------------------------------------------------
def test_a_decision_records_its_author(candidature, recruteur):
    decide(candidature, stage=Application.Stage.PHONE, note="Profil interessant", actor=recruteur)
    candidature.refresh_from_db()

    assert candidature.stage == Application.Stage.PHONE
    assert candidature.decided_by == recruteur
    assert candidature.decided_at is not None


def test_rejecting_requires_a_written_reason(candidature, recruteur):
    """« L'IA classe, elle ne rejette jamais » : le rejet se motive."""
    with pytest.raises(DecisionRefused, match="motif"):
        decide(candidature, stage=Application.Stage.REJECTED, note="", actor=recruteur)

    with pytest.raises(DecisionRefused, match="motif"):
        decide(candidature, stage=Application.Stage.REJECTED, note="non", actor=recruteur)

    candidature.refresh_from_db()
    assert candidature.stage == Application.Stage.RECEIVED


def test_rejecting_with_a_reason_is_accepted(candidature, recruteur):
    decide(
        candidature, stage=Application.Stage.REJECTED,
        note="Pas d'experience sur l'orchestration, poste tres senior.",
        actor=recruteur,
    )
    candidature.refresh_from_db()
    assert candidature.stage == Application.Stage.REJECTED
    assert candidature.is_closed


def test_advancing_does_not_require_a_reason(candidature, recruteur):
    decide(candidature, stage=Application.Stage.TECHNICAL, note="", actor=recruteur)
    candidature.refresh_from_db()
    assert candidature.stage == Application.Stage.TECHNICAL


def test_an_unknown_stage_is_refused(candidature, recruteur):
    with pytest.raises(DecisionRefused, match="Etape inconnue"):
        decide(candidature, stage="teleportation", note="x" * 20, actor=recruteur)


def test_a_decision_without_an_author_is_refused(candidature):
    with pytest.raises(DecisionRefused, match="habilite"):
        decide(candidature, stage=Application.Stage.PHONE, note="", actor=None)


def test_a_viewer_cannot_decide_even_by_calling_the_service(candidature, observateur):
    """La verification n'est pas seulement dans la vue : elle est dans le service."""
    with pytest.raises(DecisionRefused, match="habilite"):
        decide(candidature, stage=Application.Stage.PHONE, note="", actor=observateur)


def test_every_decision_is_journalised(candidature, recruteur):
    decide(candidature, stage=Application.Stage.PHONE, note="Premier echange", actor=recruteur)
    decide(candidature, stage=Application.Stage.TECHNICAL, note="Test technique", actor=recruteur)

    entrees = AuditLog.objects.filter(
        action=AuditLog.Action.STAGE_CHANGED, object_id=str(candidature.pk)
    )
    assert entrees.count() == 2
    # Le dossier ne garde que la derniere decision, le journal les garde toutes.
    assert {entree.metadata["stage"] for entree in entrees} == {"phone", "technical"}


def test_decide_view_moves_the_application(client, candidature, recruteur):
    client.force_login(recruteur)
    response = client.post(
        reverse("matching:decide", kwargs={"pk": candidature.pk}),
        {"stage": Application.Stage.PHONE, "note": "A rappeler"},
        follow=True,
    )
    assert response.status_code == 200
    candidature.refresh_from_db()
    assert candidature.stage == Application.Stage.PHONE


def test_decide_view_reports_a_missing_reason(client, candidature, recruteur):
    client.force_login(recruteur)
    response = client.post(
        reverse("matching:decide", kwargs={"pk": candidature.pk}),
        {"stage": Application.Stage.REJECTED, "note": ""},
        follow=True,
    )
    messages = [str(m) for m in response.context["messages"]]
    assert any("motif" in message for message in messages)
    candidature.refresh_from_db()
    assert candidature.stage == Application.Stage.RECEIVED


# --- Roles -------------------------------------------------------------------
MUTATIONS = [
    ("matching:decide", "application"),
    ("matching:generate_questions", "application"),
    ("matching:score_offer", "offer"),
    ("assistant:ask", "offer"),
    ("assistant:clear", "offer"),
]


@pytest.mark.parametrize(("nom", "cible"), MUTATIONS)
def test_a_viewer_is_refused_every_mutating_action(
    client, candidature, observateur, nom, cible
):
    client.force_login(observateur)
    if cible == "application":
        url = reverse(nom, kwargs={"pk": candidature.pk})
    else:
        url = reverse(nom, kwargs={"slug": candidature.offer.slug})

    reponse = client.post(url, {}, HTTP_REFERER="/", follow=True)

    assert reponse.status_code == 200
    messages = [str(m) for m in reponse.context["messages"]]
    assert any("role" in message.lower() for message in messages), (
        f"{nom} devrait refuser un compte en lecture seule"
    )


def test_a_viewer_can_still_read(client, candidature, observateur):
    """Le controle porte sur l'ecriture : consulter reste ouvert."""
    client.force_login(observateur)
    for url in [
        reverse("candidates:dashboard"),
        reverse("candidates:application_detail", kwargs={"pk": candidature.pk}),
        reverse("matching:ranking", kwargs={"slug": candidature.offer.slug}),
    ]:
        assert client.get(url).status_code == 200


def test_a_refused_action_is_journalised(client, candidature, observateur):
    """Une tentative refusee interesse un auditeur autant qu'une action reussie."""
    client.force_login(observateur)
    client.post(
        reverse("matching:decide", kwargs={"pk": candidature.pk}),
        {"stage": Application.Stage.REJECTED, "note": "x" * 20},
        HTTP_REFERER="/", follow=True,
    )
    entree = AuditLog.objects.filter(summary__startswith="Action refusee").latest(
        "created_at"
    )
    assert entree.actor == observateur
    assert entree.metadata["role"] == "viewer"


def test_the_decision_form_is_hidden_from_a_viewer(client, candidature, observateur):
    client.force_login(observateur)
    contenu = client.get(
        reverse("candidates:application_detail", kwargs={"pk": candidature.pk})
    ).content.decode()
    assert "lecture seule" in contenu
    assert 'name="stage"' not in contenu


# --- Conservation des donnees ------------------------------------------------
def test_a_retention_date_is_set_on_creation(db, settings):
    settings.DATA_RETENTION_DAYS = 90
    candidat = Candidate.objects.create(full_name="Test", email="t@example.com")

    assert candidat.retention_until == dt.date.today() + dt.timedelta(days=90)
    assert candidat.days_until_purge == 90
    assert not candidat.retention_expired


def test_an_explicit_retention_date_is_kept(db):
    echeance = dt.date.today() + dt.timedelta(days=5)
    candidat = Candidate.objects.create(
        full_name="Test", email="t@example.com", retention_until=echeance
    )
    assert candidat.retention_until == echeance


def test_expired_and_expiring_are_distinguished(db):
    Candidate.objects.create(
        full_name="Echu", email="a@example.com",
        retention_until=dt.date.today() - dt.timedelta(days=1),
    )
    Candidate.objects.create(
        full_name="Bientot", email="b@example.com",
        retention_until=dt.date.today() + dt.timedelta(days=10),
    )
    Candidate.objects.create(
        full_name="Loin", email="c@example.com",
        retention_until=dt.date.today() + dt.timedelta(days=300),
    )

    assert [c.full_name for c in retention.expired()] == ["Echu"]
    assert [c.full_name for c in retention.expiring_soon()] == ["Bientot"]


def test_dry_run_destroys_nothing(db):
    Candidate.objects.create(
        full_name="Echu", email="a@example.com",
        retention_until=dt.date.today() - dt.timedelta(days=1),
    )
    rapport = retention.purge(dry_run=True)

    assert rapport.due == 1
    assert rapport.deleted == 0
    assert Candidate.objects.count() == 1


def test_purge_deletes_the_whole_file(db, offre):
    """La suppression est en cascade : rien ne doit survivre du dossier."""
    candidat = Candidate.objects.create(
        full_name="Echu", email="a@example.com",
        retention_until=dt.date.today() - dt.timedelta(days=1),
    )
    CandidateSkill.objects.create(candidate=candidat, name="Python")
    CVDocument.objects.create(
        candidate=candidat, original_filename="cv.pdf", content_hash="abc"
    )
    Application.objects.create(candidate=candidat, offer=offre)

    rapport = retention.purge()

    assert rapport.deleted == 1
    assert Candidate.objects.count() == 0
    assert CandidateSkill.objects.count() == 0
    assert CVDocument.objects.count() == 0
    assert Application.objects.count() == 0


def test_purge_spares_files_still_within_their_term(db):
    Candidate.objects.create(
        full_name="Valide", email="v@example.com",
        retention_until=dt.date.today() + dt.timedelta(days=30),
    )
    retention.purge()
    assert Candidate.objects.count() == 1


def test_purge_is_journalised_without_personal_data(db):
    candidat = Candidate.objects.create(
        full_name="Alice Martin", email="alice@example.com",
        retention_until=dt.date.today() - dt.timedelta(days=1),
    )
    identifiant = str(candidat.pk)
    retention.purge()

    entree = AuditLog.objects.get(action=AuditLog.Action.DATA_PURGED)
    trace = entree.summary + str(entree.metadata)
    assert identifiant in trace
    # Ni le nom ni l'adresse ne doivent subsister dans le journal.
    assert "Alice" not in trace
    assert "alice@example.com" not in trace


def test_purge_on_an_empty_base_is_harmless(db):
    rapport = retention.purge()
    assert rapport.nothing_to_do
    assert not AuditLog.objects.filter(action=AuditLog.Action.DATA_PURGED).exists()


def test_the_dashboard_reports_the_retention_state(client, db, recruteur, settings):
    settings.DATA_RETENTION_DAYS = 365
    Candidate.objects.create(
        full_name="Echu", email="a@example.com",
        retention_until=dt.date.today() - dt.timedelta(days=1),
    )
    client.force_login(recruteur)
    contexte = client.get(reverse("candidates:dashboard")).context["retention"]

    assert contexte["days"] == 365
    assert contexte["expired"] == 1


def test_the_periodic_task_runs_the_purge(db):
    from apps.candidates.tasks import purge_expired_task

    Candidate.objects.create(
        full_name="Echu", email="a@example.com",
        retention_until=dt.date.today() - dt.timedelta(days=1),
    )
    assert purge_expired_task() == 1
    assert Candidate.objects.count() == 0
