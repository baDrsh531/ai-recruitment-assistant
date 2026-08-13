"""Rejeu des decisions, et journal d'audit consultable.

Le rejeu est le seul controle du projet qui eprouve la reproductibilite sur des
**decisions reelles** plutot que sur un jeu annote. Ces tests portent donc moins
sur le calcul — deja couvert ailleurs — que sur l'**attribution** : savoir a qui
imputer un ecart, et refuser de conclure quand on ne peut pas.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.core.models import AuditLog
from apps.evaluation import replay
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching.models import MatchScore


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


@pytest.fixture
def offre(db):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    JobSkill.objects.create(offer=offre, name="Django", requirement="required")
    return offre


def _dossier_tranche(
    offre,
    recruteur,
    nom="Alice",
    competences=("Python", "Django"),
    *,
    il_y_a_jours=30,
    version=engine.ENGINE_VERSION,
    score=None,
    overridden=None,
):
    """Un dossier score puis tranche, avec des dates maitrisees."""
    candidat = Candidate.objects.create(full_name=nom, total_experience_years=5)
    for competence in competences:
        CandidateSkill.objects.create(
            candidate=candidat, name=competence, years=5, last_used_year=2026
        )
    candidature = Application.objects.create(candidate=candidat, offer=offre)

    decide_le = timezone.now() - dt.timedelta(days=il_y_a_jours)
    calcule = engine.score(candidat, offre)
    enregistre = MatchScore.objects.create(
        application=candidature,
        overall=calcule.overall if score is None else score,
        engine_version=version,
        weights_used=calcule.weights_used,
        breakdown={},
        skill_matches=[],
        gaps=[],
        overridden_score=overridden,
        overridden_by=recruteur if overridden is not None else None,
    )
    MatchScore.objects.filter(pk=enregistre.pk).update(
        created_at=decide_le - dt.timedelta(hours=1)
    )
    # Antidater aussi les donnees : creees a l'instant, elles seraient toutes
    # « modifiees depuis » une decision posee dans le passe, et chaque rejeu
    # sortirait non concluant. `updated_at` est en `auto_now`, il faut donc
    # passer par une mise a jour de requete.
    avant = decide_le - dt.timedelta(days=1)
    Candidate.objects.filter(pk=candidat.pk).update(updated_at=avant)
    JobOffer.objects.filter(pk=offre.pk).update(updated_at=avant)
    CandidateSkill.objects.filter(candidate=candidat).update(updated_at=avant)
    JobSkill.objects.filter(offer=offre).update(updated_at=avant)
    Application.objects.filter(pk=candidature.pk).update(
        stage=Application.Stage.REJECTED,
        decided_by=recruteur,
        decided_at=decide_le,
        decision_note="Motif.",
    )
    candidature.refresh_from_db()
    return candidature


# --- Ce que le rejeu affirme --------------------------------------------------
def test_the_same_engine_on_the_same_data_gives_the_same_score(offre, recruteur):
    """L'affirmation exacte que le projet fait partout."""
    _dossier_tranche(offre, recruteur)

    rapport = replay.rejouer()

    assert rapport.total == 1
    assert len(rapport.identiques) == 1
    assert rapport.reproductible
    assert "exactement ce qu'il rendait alors" in rapport.lecture


def test_a_divergence_at_equal_engine_version_is_a_defect(
    offre, recruteur, monkeypatch
):
    """Une divergence entre deux versions est attendue et documentee. A version
    egale, c'est un defaut de reproductibilite — la distinction porte tout le
    module."""
    _dossier_tranche(offre, recruteur)

    vrai = replay.calculer

    def _decale(candidat, offre_, **kwargs):
        resultat = vrai(candidat, offre_, **kwargs)
        object.__setattr__(resultat, "overall", resultat.overall - 0.1)
        return resultat

    monkeypatch.setattr(replay, "calculer", _decale)

    rapport = replay.rejouer()

    assert not rapport.reproductible
    assert "pas reproductible" in rapport.lecture
    assert "un defaut" in rapport.lecture


def test_an_engine_change_is_attributed_to_its_transition(offre, recruteur):
    _dossier_tranche(offre, recruteur, version="1.1.0", score=0.40)

    rapport = replay.rejouer()

    assert rapport.reproductible, "une version differente n'est pas un defaut"
    assert len(rapport.divergents) == 1
    transition = rapport.par_transition[0]
    assert (transition["de"], transition["vers"]) == ("1.1.0", engine.ENGINE_VERSION)
    assert transition["nombre"] == 1


# --- Ce que le rejeu refuse de conclure ---------------------------------------
def test_data_changed_since_the_decision_makes_the_replay_inconclusive(
    offre, recruteur
):
    """Un score qui bouge peut venir du moteur ou de la donnee. Confondre les
    deux donnerait un chiffre flatteur ou alarmant selon le sens du vent."""
    candidature = _dossier_tranche(offre, recruteur)
    CandidateSkill.objects.create(
        candidate=candidature.candidate, name="PostgreSQL", years=3,
        last_used_year=2026,
    )

    rapport = replay.rejouer()

    assert len(rapport.non_concluants) == 1
    assert rapport.concluants == []
    assert "aucune exploitable" in rapport.lecture


def test_an_offer_changed_since_the_decision_is_inconclusive_too(offre, recruteur):
    candidature = _dossier_tranche(offre, recruteur)
    JobSkill.objects.create(
        offer=candidature.offer, name="Kubernetes", requirement="preferred"
    )

    assert len(replay.rejouer().non_concluants) == 1


def test_a_decision_taken_without_any_score_is_counted_apart(offre, recruteur):
    """Rien a comparer — mais compte, pas ignore en silence."""
    candidat = Candidate.objects.create(full_name="Sans score")
    candidature = Application.objects.create(candidate=candidat, offer=offre)
    Application.objects.filter(pk=candidature.pk).update(
        stage=Application.Stage.REJECTED, decided_by=recruteur,
        decided_at=timezone.now() - dt.timedelta(days=5),
    )

    rapport = replay.rejouer()

    assert rapport.sans_score == 1
    assert rapport.total == 0


def test_a_score_computed_after_the_decision_is_not_used(offre, recruteur):
    """Rejouer contre un score posterieur comparerait le moteur a lui-meme."""
    candidature = _dossier_tranche(offre, recruteur, version="1.1.0", score=0.40)
    posterieur = MatchScore.objects.create(
        application=candidature, overall=0.99,
        engine_version=engine.ENGINE_VERSION, breakdown={},
    )
    MatchScore.objects.filter(pk=posterieur.pk).update(created_at=timezone.now())

    rejeu = replay.rejouer().rejeux[0]

    assert rejeu.score_alors == pytest.approx(0.40)
    assert rejeu.version_alors == "1.1.0"


# --- Ce qui coute vraiment quelque chose --------------------------------------
def test_crossing_the_threshold_is_reported_as_a_flip(offre, recruteur, monkeypatch):
    """Un dossier qui passe de 0.91 a 0.90 n'a rien change ; un dossier qui
    passe sous le seuil a tout change."""
    from apps.evaluation import threshold as calibration

    monkeypatch.setattr(calibration, "recommended_threshold", lambda **k: 0.60)
    _dossier_tranche(offre, recruteur, version="1.1.0", score=0.55)

    rapport = replay.rejouer()

    assert len(rapport.bascules) == 1
    assert rapport.rejeux[0].gravite == "bascule"


def test_a_small_move_that_stays_on_one_side_is_not_a_flip(
    offre, recruteur, monkeypatch
):
    from apps.evaluation import threshold as calibration

    monkeypatch.setattr(calibration, "recommended_threshold", lambda **k: 0.10)
    _dossier_tranche(offre, recruteur, version="1.1.0", score=0.80)

    rapport = replay.rejouer()

    assert rapport.divergents
    assert rapport.bascules == []
    assert rapport.rejeux[0].gravite == "ecart"


def test_a_manually_corrected_score_is_compared_engine_to_engine(offre, recruteur):
    """Confronter un chiffre humain a un chiffre calcule ferait apparaitre tout
    dossier corrige comme une divergence du moteur."""
    _dossier_tranche(offre, recruteur, overridden=0.10)

    rejeu = replay.rejouer().rejeux[0]

    assert rejeu.corrige_a_la_main
    assert rejeu.identique, "le moteur n'a pas bouge, seule la main l'a fait"
    assert not rejeu.bascule


def test_two_runs_of_the_engine_agree_within_the_tolerance(offre, recruteur):
    """La tolerance existe pour le bruit des flottants, pas pour masquer un
    ecart : elle doit rester tres en dessous de ce qui ferait basculer."""
    assert replay.TOLERANCE < 0.005

    _dossier_tranche(offre, recruteur)
    premier = replay.rejouer().rejeux[0].score_maintenant
    second = replay.rejouer().rejeux[0].score_maintenant

    assert abs(premier - second) < replay.TOLERANCE


def test_the_command_runs_and_strict_fails_on_a_real_defect(
    offre, recruteur, monkeypatch
):
    _dossier_tranche(offre, recruteur)
    call_command("replay_decisions")

    vrai = replay.calculer

    def _decale(candidat, offre_, **kwargs):
        resultat = vrai(candidat, offre_, **kwargs)
        object.__setattr__(resultat, "overall", resultat.overall - 0.1)
        return resultat

    monkeypatch.setattr(replay, "calculer", _decale)
    with pytest.raises(SystemExit):
        call_command("replay_decisions", "--strict")


def test_the_replay_page_renders(client, offre, recruteur):
    _dossier_tranche(offre, recruteur, version="1.1.0", score=0.40)
    client.force_login(recruteur)

    contenu = client.get("/transparence/rejeu/").content.decode()

    assert "Le score est-il reproductible" in contenu
    assert "Ce qui a fait bouger les scores" in contenu
    assert "1.1.0" in contenu


# --- Le journal d'audit, enfin consultable ------------------------------------
def test_the_audit_trail_lists_entries(client, offre, recruteur):
    _dossier_tranche(offre, recruteur)
    client.force_login(recruteur)

    reponse = client.get("/transparence/journal/")

    assert reponse.status_code == 200
    assert "Journal d'audit" in reponse.content.decode()


def test_the_trail_can_be_narrowed_to_one_object(client, offre, recruteur):
    """« Montrez-moi tout ce qui est arrive a ce candidat » est la premiere
    demande d'un auditeur comme d'un candidat exercant son droit d'acces."""
    from apps.core.services import record_audit

    candidature = _dossier_tranche(offre, recruteur)
    record_audit(
        AuditLog.Action.CANDIDATE_VIEWED, actor=recruteur, obj=candidature,
        summary="Consultation du dossier",
    )
    record_audit(
        AuditLog.Action.DATA_EXPORTED, actor=recruteur, summary="Autre objet",
    )
    client.force_login(recruteur)

    contenu = client.get(
        f"/transparence/journal/?objet={candidature.pk}"
    ).content.decode()

    assert "Consultation du dossier" in contenu
    assert "Autre objet" not in contenu


def test_the_trail_separates_the_machine_from_the_human(client, offre, recruteur):
    """La distinction sur laquelle repose l'exigence de supervision humaine."""
    from apps.core.services import record_audit

    # Libelles sans apostrophe : `{{ entree.summary }}` est une variable, donc
    # Django l'echappe, et « Decision d'un humain » sort en « d&#x27;un ». Une
    # assertion posee dessus echouerait sur une page parfaitement correcte.
    record_audit(
        AuditLog.Action.AGENT_RECOMMENDED, actor=recruteur,
        summary="Proposition de la machine", agent=True,
    )
    record_audit(
        AuditLog.Action.STAGE_CHANGED, actor=recruteur,
        summary="Decision prise par un humain",
    )
    client.force_login(recruteur)

    machine = client.get("/transparence/journal/?machine=1").content.decode()
    humain = client.get("/transparence/journal/?machine=0").content.decode()

    assert "Proposition de la machine" in machine
    assert "Decision prise par un humain" not in machine
    assert "Decision prise par un humain" in humain
    assert "Proposition de la machine" not in humain


def test_the_trail_can_be_filtered_by_action(client, offre, recruteur):
    from apps.core.services import record_audit

    record_audit(
        AuditLog.Action.DATA_EXPORTED, actor=recruteur, summary="Un export",
    )
    record_audit(
        AuditLog.Action.CANDIDATE_VIEWED, actor=recruteur, summary="Une lecture",
    )
    client.force_login(recruteur)

    contenu = client.get(
        "/transparence/journal/?action=data_exported"
    ).content.decode()

    assert "Un export" in contenu
    assert "Une lecture" not in contenu


def test_the_trail_stays_readable_when_the_actor_was_deleted(
    client, offre, recruteur, django_user_model
):
    """`on_delete=SET_NULL` : l'entree survit au compte, et la page doit le
    dire au lieu de tomber."""
    from apps.core.services import record_audit

    parti = django_user_model.objects.create_user(
        username="parti", password="mot-de-passe-de-test-123", role="recruiter"
    )
    record_audit(
        AuditLog.Action.DATA_EXPORTED, actor=parti,
        summary="Trace laissee par un compte disparu",
    )
    parti.delete()
    client.force_login(recruteur)

    contenu = client.get("/transparence/journal/").content.decode()

    assert "Trace laissee par un compte disparu" in contenu
    assert "compte supprime" in contenu, "la page doit nommer l'auteur manquant"
