"""Tests de l'accord entre recruteurs.

Le kappa est facile a calculer et facile a mal lire. Les tests portent donc
autant sur le calcul que sur la retenue : ne rien afficher quand l'echantillon
est trop petit, et distinguer un accord reel d'un accord de hasard.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.evaluation import agreement
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching.services import decide, score_application


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture
def offre(db):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    return offre


def _recruteur(django_user_model, nom):
    return django_user_model.objects.create_user(
        username=nom, password="mot-de-passe-de-test-123", role="recruiter"
    )


def _candidature(offre, nom, annees=5):
    candidat = Candidate.objects.create(full_name=nom, total_experience_years=annees)
    CandidateSkill.objects.create(
        candidate=candidat, name="Python", years=annees, last_used_year=2026
    )
    return Application.objects.create(candidate=candidat, offer=offre)


# --- Le calcul ---------------------------------------------------------------
def test_perfect_agreement_gives_one():
    assert agreement.cohen_kappa([True, False, True], [True, False, True]) == 1.0


def test_total_disagreement_is_negative():
    valeur = agreement.cohen_kappa([True, False, True, False], [False, True, False, True])
    assert valeur < 0


def test_chance_agreement_gives_about_zero():
    """Deux series independantes tombent d'accord par hasard : kappa proche de 0."""
    a = [True, True, False, False] * 5
    b = [True, False, True, False] * 5
    assert abs(agreement.cohen_kappa(a, b)) < 0.01


def test_a_high_raw_agreement_can_hide_a_null_kappa():
    """Le resultat qui justifie le kappa plutot qu'un pourcentage.

    Neuf dossiers ecartes sur dix : deux recruteurs sont d'accord 80 % du temps
    tout en n'ayant aucun accord au-dela du hasard.
    """
    a = [False] * 8 + [True, False]
    b = [False] * 8 + [False, True]

    brut = sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)
    assert brut == 0.8
    assert agreement.cohen_kappa(a, b) < 0.0


def test_everyone_rejected_by_both_is_full_agreement():
    """Cas le plus banal en recrutement : l'accord attendu vaut 1.

    Le kappa serait indefini (0/0) ; on rend 1.0 plutot que de lever une
    exception sur le cas courant.
    """
    assert agreement.cohen_kappa([False] * 6, [False] * 6) == 1.0


def test_mismatched_series_return_zero():
    assert agreement.cohen_kappa([True, False], [True]) == 0.0
    assert agreement.cohen_kappa([], []) == 0.0


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [(0.9, "presque parfait"), (0.7, "substantiel"), (0.5, "modere"),
     (0.3, "faible"), (0.1, "leger"), (-0.2, "nul ou pire que le hasard")],
)
def test_the_reading_follows_the_usual_scale(valeur, attendu):
    assert agreement.libelle_kappa(valeur) == attendu


# --- Lecture des decisions ---------------------------------------------------
def test_only_the_last_decision_of_a_pair_counts(db, offre, django_user_model):
    """Un recruteur qui hesite ne doit pas peser plus qu'un recruteur decide."""
    rh = _recruteur(django_user_model, "rh")
    candidature = _candidature(offre, "Alice")

    decide(candidature, stage="screening", note="", actor=rh)
    decide(candidature, stage="rejected", note="Finalement non, trop junior", actor=rh)

    rapport = agreement.analyse(threshold=0.5)
    profil = next(item for item in rapport.recruteurs if item.nom == "rh")

    assert profil.decisions == 1
    assert profil.retenus == 0


def test_a_withdrawal_counts_as_not_retained(db, offre, django_user_model):
    rh = _recruteur(django_user_model, "rh")
    candidature = _candidature(offre, "Alice")
    decide(candidature, stage="withdrawn", note="Candidat s'est desiste", actor=rh)

    profil = agreement.analyse(threshold=0.5).recruteurs[0]
    assert profil.retenus == 0


def test_two_recruiters_on_the_same_files_are_compared(db, offre, django_user_model):
    premier = _recruteur(django_user_model, "premier")
    second = _recruteur(django_user_model, "second")

    for index in range(6):
        candidature = _candidature(offre, f"Candidat {index}")
        decide(candidature, stage="screening", note="", actor=premier)
        decide(candidature, stage="screening", note="", actor=second)

    rapport = agreement.analyse(threshold=0.5)
    assert len(rapport.paires) == 1
    paire = rapport.paires[0]
    assert paire.commun == 6
    assert paire.kappa == 1.0
    assert paire.interpretable


def test_recruiters_without_common_files_are_not_compared(db, offre, django_user_model):
    premier = _recruteur(django_user_model, "premier")
    second = _recruteur(django_user_model, "second")

    decide(_candidature(offre, "A"), stage="screening", note="", actor=premier)
    decide(_candidature(offre, "B"), stage="screening", note="", actor=second)

    rapport = agreement.analyse(threshold=0.5)
    assert rapport.paires == []
    assert not rapport.mesurable


def test_too_few_common_files_is_declared_not_interpretable(db, offre, django_user_model):
    """Sur trois dossiers, le kappa passe de 0 a 1 par accident."""
    premier = _recruteur(django_user_model, "premier")
    second = _recruteur(django_user_model, "second")

    for index in range(3):
        candidature = _candidature(offre, f"Candidat {index}")
        decide(candidature, stage="screening", note="", actor=premier)
        decide(candidature, stage="screening", note="", actor=second)

    rapport = agreement.analyse(threshold=0.5)
    assert rapport.paires[0].commun == 3
    assert not rapport.paires[0].interpretable
    assert not rapport.mesurable


def test_disagreeing_pairs_are_listed_first(db, offre, django_user_model):
    """C'est le desaccord qui merite une conversation, pas l'accord."""
    accord_a = _recruteur(django_user_model, "accord_a")
    accord_b = _recruteur(django_user_model, "accord_b")
    desaccord = _recruteur(django_user_model, "desaccord")

    for index in range(6):
        candidature = _candidature(offre, f"Candidat {index}")
        retenue = "screening" if index % 2 else "rejected"
        inverse = "rejected" if index % 2 else "screening"
        decide(candidature, stage=retenue, note="motif suffisant", actor=accord_a)
        decide(candidature, stage=retenue, note="motif suffisant", actor=accord_b)
        decide(candidature, stage=inverse, note="motif suffisant", actor=desaccord)

    rapport = agreement.analyse(threshold=0.5)
    assert rapport.paires[0].kappa < rapport.paires[-1].kappa


# --- Ecart au score ----------------------------------------------------------
def test_following_the_score_is_measured(db, offre, django_user_model):
    rh = _recruteur(django_user_model, "rh")
    fort = _candidature(offre, "Fort", annees=8)
    faible = _candidature(offre, "Faible", annees=0)
    CandidateSkill.objects.filter(candidate=faible.candidate).delete()
    for candidature in (fort, faible):
        score_application(candidature, with_explanation=False)

    decide(fort, stage="screening", note="", actor=rh)
    decide(faible, stage="rejected", note="Ne couvre pas le poste", actor=rh)

    profil = agreement.analyse(threshold=0.5).recruteurs[0]
    assert profil.suivis == 2
    assert profil.ecarts == 0
    assert profil.accord_avec_le_score == 1.0


def test_departing_from_the_score_is_measured(db, offre, django_user_model):
    rh = _recruteur(django_user_model, "rh")
    fort = _candidature(offre, "Fort", annees=8)
    score_application(fort, with_explanation=False)

    decide(fort, stage="rejected", note="Entretien decevant, malgre le score", actor=rh)

    profil = agreement.analyse(threshold=0.5).recruteurs[0]
    assert profil.ecarts == 1
    assert profil.accord_avec_le_score == 0.0


def test_an_unscored_file_is_left_out_of_the_comparison(db, offre, django_user_model):
    """Sans score, il n'y a rien a comparer : le dossier n'est pas compte."""
    rh = _recruteur(django_user_model, "rh")
    candidature = _candidature(offre, "Alice")
    decide(candidature, stage="screening", note="", actor=rh)

    profil = agreement.analyse(threshold=0.5).recruteurs[0]
    assert profil.decisions == 1
    assert profil.suivis + profil.ecarts == 0
    assert not profil.mesurable


def test_the_spread_between_recruiters_is_reported(db, offre, django_user_model):
    severe = _recruteur(django_user_model, "severe")
    indulgent = _recruteur(django_user_model, "indulgent")

    for index in range(6):
        candidature = _candidature(offre, f"Candidat {index}")
        decide(candidature, stage="rejected", note="motif suffisant", actor=severe)
        decide(candidature, stage="screening", note="", actor=indulgent)

    rapport = agreement.analyse(threshold=0.5)
    assert rapport.ecart_maximal == 1.0


# --- Rien n'est modifie ------------------------------------------------------
def test_the_analysis_changes_nothing(db, offre, django_user_model):
    rh = _recruteur(django_user_model, "rh")
    candidature = _candidature(offre, "Alice")
    decide(candidature, stage="screening", note="", actor=rh)

    agreement.analyse(threshold=0.5)
    candidature.refresh_from_db()

    assert candidature.stage == "screening"


def test_an_empty_base_is_not_an_error(db):
    rapport = agreement.analyse(threshold=0.5)

    assert rapport.recruteurs == []
    assert rapport.paires == []
    assert not rapport.mesurable
    assert rapport.ecart_maximal == 0.0


def test_the_report_serialises(db, offre, django_user_model):
    rh = _recruteur(django_user_model, "rh")
    decide(_candidature(offre, "Alice"), stage="screening", note="", actor=rh)

    donnees = agreement.analyse(threshold=0.5).as_dict()
    assert set(donnees) >= {"seuil", "mesurable", "recruteurs", "paires"}


# --- Page --------------------------------------------------------------------
def test_the_page_renders(client, db, offre, django_user_model):
    rh = _recruteur(django_user_model, "rh")
    decide(_candidature(offre, "Alice"), stage="screening", note="", actor=rh)

    client.force_login(rh)
    reponse = client.get(reverse("evaluation:agreement"))

    assert reponse.status_code == 200
    assert reponse.context["rapport"].recruteurs


def test_the_page_says_it_notes_nobody(client, db, django_user_model):
    rh = _recruteur(django_user_model, "rh")
    client.force_login(rh)
    contenu = client.get(reverse("evaluation:agreement")).content.decode()

    # Fragments courts, sans retour a la ligne du gabarit : le HTML rendu
    # conserve les sauts de ligne du source, et une phrase qui tient sur deux
    # lignes ne s'y retrouve pas telle quelle.
    assert "ne note personne" in contenu
    assert "le score a vu un PDF" in contenu


def test_the_page_refuses_to_show_an_unreadable_kappa(client, db, offre, django_user_model):
    premier = _recruteur(django_user_model, "premier")
    second = _recruteur(django_user_model, "second")
    candidature = _candidature(offre, "Alice")
    decide(candidature, stage="screening", note="", actor=premier)
    decide(candidature, stage="screening", note="", actor=second)

    client.force_login(premier)
    contenu = client.get(reverse("evaluation:agreement")).content.decode()

    assert "non mesurable" in contenu
    assert "par accident" in contenu
