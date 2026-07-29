"""Tests de la comparaison entre candidats.

Entierement deterministe : aucun appel modele, ni reel ni simule.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import comparison, engine
from apps.matching.services import score_application


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture
def offre(db):
    offer = JobOffer.objects.create(
        title="Backend Python", description="x", experience_min_years=2,
        status=JobOffer.Status.OPEN,
    )
    JobSkill.objects.create(offer=offer, name="Python", weight=2.0, min_years=2)
    JobSkill.objects.create(offer=offer, name="Django", weight=1.5, min_years=1)
    JobSkill.objects.create(offer=offer, name="Kubernetes", weight=1.0)
    return offer


def _candidat(offre, nom, competences, annees=4.0):
    candidate = Candidate.objects.create(
        full_name=nom, email=f"{nom.lower()}@example.com",
        total_experience_years=annees,
    )
    for name, years in competences:
        CandidateSkill.objects.create(
            candidate=candidate, name=name, years=years, last_used_year=2026
        )
    Application.objects.create(candidate=candidate, offer=offre)
    return candidate


# --- Construction ------------------------------------------------------------
def test_comparison_builds_one_column_per_candidate(offre):
    a = _candidat(offre, "Alice", [("Python", 5), ("Django", 4)])
    b = _candidat(offre, "Bob", [("Python", 5), ("Kubernetes", 3)])

    resultat = comparison.compare(offre, [a, b])

    assert [colonne.candidate for colonne in resultat.columns] == [a, b]
    assert all(len(row.cells) == 2 for row in resultat.skills)
    assert all(len(row.cells) == 2 for row in resultat.criteria)


def test_comparison_caps_the_number_of_candidates(offre):
    candidats = [
        _candidat(offre, f"C{index}", [("Python", 3)]) for index in range(6)
    ]
    resultat = comparison.compare(offre, candidats)
    assert len(resultat.columns) == comparison.MAX_CANDIDATES


def test_required_skills_come_first(offre):
    a = _candidat(offre, "Alice", [("Python", 5)])
    resultat = comparison.compare(offre, [a])
    obligatoires = [row.is_required for row in resultat.skills]
    # Toutes les obligatoires precedent les souhaitees.
    assert obligatoires == sorted(obligatoires, reverse=True)


# --- Ce qui differencie ------------------------------------------------------
def test_shared_skills_are_separated_from_discriminating_ones(offre):
    """Une ligne ou tout le monde est a egalite n'apprend rien."""
    a = _candidat(offre, "Alice", [("Python", 5), ("Django", 4)])
    b = _candidat(offre, "Bob", [("Python", 5)])

    resultat = comparison.compare(offre, [a, b])
    departageantes = {row.skill for row in resultat.discriminating_skills}
    communes = {row.skill for row in resultat.shared_skills}

    assert "Django" in departageantes  # Alice l'a, Bob non
    assert "Python" in communes  # les deux au meme niveau
    assert not departageantes & communes


def test_best_is_not_marked_when_everyone_ties(offre):
    """Designer un meilleur sur un ecart negligeable serait trompeur."""
    a = _candidat(offre, "Alice", [("Python", 5)])
    b = _candidat(offre, "Bob", [("Python", 5)])

    resultat = comparison.compare(offre, [a, b])
    python = next(row for row in resultat.skills if row.skill == "Python")
    assert not any(cell.best for cell in python.cells)


def test_best_is_marked_when_the_gap_is_real(offre):
    a = _candidat(offre, "Alice", [("Python", 5), ("Django", 4)])
    b = _candidat(offre, "Bob", [("Python", 5)])

    resultat = comparison.compare(offre, [a, b])
    django = next(row for row in resultat.skills if row.skill == "Django")
    assert django.cells[0].best
    assert not django.cells[1].best


def test_strengths_list_what_a_candidate_alone_brings(offre):
    a = _candidat(offre, "Alice", [("Python", 5), ("Django", 4)])
    b = _candidat(offre, "Bob", [("Python", 5), ("Kubernetes", 3)])

    resultat = comparison.compare(offre, [a, b])
    forces = {colonne.candidate.full_name: colonne.strengths for colonne in resultat.columns}

    assert "Django" in forces["Alice"]
    assert "Kubernetes" in forces["Bob"]
    assert "Kubernetes" not in forces["Alice"]


def test_gaps_are_reported_per_candidate(offre):
    a = _candidat(offre, "Alice", [("Python", 5)])
    resultat = comparison.compare(offre, [a])
    assert "Django" in resultat.columns[0].gaps


def test_comparison_is_deterministic(offre):
    a = _candidat(offre, "Alice", [("Python", 5), ("Django", 4)])
    b = _candidat(offre, "Bob", [("Python", 3)])

    premier = comparison.compare(offre, [a, b])
    second = comparison.compare(offre, [a, b])

    assert [c.overall for c in premier.columns] == [c.overall for c in second.columns]
    assert [row.skill for row in premier.skills] == [row.skill for row in second.skills]


def test_empty_selection_is_handled(offre):
    resultat = comparison.compare(offre, [])
    assert resultat.columns == []
    assert resultat.skills == []


# --- Interface ---------------------------------------------------------------
@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123"
    )


def test_comparison_view_requires_login(client, offre):
    url = reverse("matching:comparison", kwargs={"slug": offre.slug})
    assert client.get(url).status_code == 302


def test_comparison_view_defaults_to_the_ranking(client, offre, recruteur):
    """Sans selection, on compare les premiers du classement."""
    for nom, competences in [
        ("Alice", [("Python", 5), ("Django", 4)]),
        ("Bob", [("Python", 3)]),
        ("Carla", [("Django", 2)]),
    ]:
        candidat = _candidat(offre, nom, competences)
        score_application(
            Application.objects.get(candidate=candidat, offer=offre),
            with_explanation=False,
        )

    client.force_login(recruteur)
    response = client.get(reverse("matching:comparison", kwargs={"slug": offre.slug}))

    assert response.status_code == 200
    colonnes = response.context["comparison"].columns
    assert len(colonnes) == 3
    # L'ordre suit le classement, du meilleur au moins bon.
    scores = [colonne.overall for colonne in colonnes]
    assert scores == sorted(scores, reverse=True)


def test_comparison_view_honours_an_explicit_selection(client, offre, recruteur):
    alice = _candidat(offre, "Alice", [("Python", 5), ("Django", 4)])
    _candidat(offre, "Bob", [("Python", 3)])
    for application in Application.objects.all():
        score_application(application, with_explanation=False)

    client.force_login(recruteur)
    url = reverse("matching:comparison", kwargs={"slug": offre.slug})
    response = client.get(url, {"c": [str(alice.pk)]})

    colonnes = response.context["comparison"].columns
    assert [colonne.candidate for colonne in colonnes] == [alice]


def test_comparison_view_ignores_candidates_from_another_offer(client, offre, recruteur):
    """Un identifiant glisse dans l'URL ne doit pas exposer un autre dossier."""
    autre = JobOffer.objects.create(title="Autre", description="x")
    JobSkill.objects.create(offer=autre, name="Go")
    intrus = _candidat(autre, "Intrus", [("Go", 3)])

    alice = _candidat(offre, "Alice", [("Python", 5)])
    for application in Application.objects.filter(offer=offre):
        score_application(application, with_explanation=False)

    client.force_login(recruteur)
    response = client.get(
        reverse("matching:comparison", kwargs={"slug": offre.slug}),
        {"c": [str(alice.pk), str(intrus.pk)]},
    )

    retenus = [c.candidate for c in response.context["comparison"].columns]
    assert intrus not in retenus
    assert alice in retenus


def test_comparison_page_renders_the_matrix(client, offre, recruteur):
    _candidat(offre, "Alice", [("Python", 5), ("Django", 4)])
    _candidat(offre, "Bob", [("Python", 5)])
    for application in Application.objects.all():
        score_application(application, with_explanation=False)

    client.force_login(recruteur)
    contenu = client.get(
        reverse("matching:comparison", kwargs={"slug": offre.slug})
    ).content.decode()

    assert "Ce qui les differencie" in contenu
    assert "Alice" in contenu and "Bob" in contenu
    assert "Django" in contenu
