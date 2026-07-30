"""Tests des graphiques.

Deux exigences tenues ici : les jeux de donnees sont corrects et sans surprise,
et **chaque valeur reste lisible sans JavaScript** — le tableau equivalent est
rendu par le serveur, pas par le navigateur.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse

from apps.candidates.models import (
    Application,
    Candidate,
    CandidateLanguage,
    CandidateSkill,
)
from apps.core import charts
from apps.jobs.models import JobOffer, JobSkill
from apps.matching.services import score_application


# --- Construction des jeux de donnees ---------------------------------------
def test_bar_sorts_by_value():
    chart = charts.bar("c", "T", [("a", 1), ("b", 5), ("c", 3)])
    assert [row["label"] for row in chart.rows] == ["b", "c", "a"]


def test_bar_folds_the_tail_visibly():
    """Le repli doit se voir, et ne pas se confondre avec la valeur.

    « Autres (15) » sur une barre valant 15 se lisait comme un doublon : le
    premier nombre compte des categories, le second des candidats. Ecrits de la
    meme facon, ils se confondaient.
    """
    pairs = [(f"s{index}", 10 - index) for index in range(12)]
    chart = charts.bar("c", "T", pairs, top=5, other_label="autres competences")

    assert len(chart.rows) == 5
    dernier = chart.rows[-1]
    assert dernier["label"] == "8 autres competences"
    # Aucune valeur n'est perdue.
    assert sum(row["values"][0] for row in chart.rows) == sum(v for _, v in pairs)


def test_bar_keeps_everything_below_the_cap():
    chart = charts.bar("c", "T", [("a", 1), ("b", 2)], top=8)
    assert len(chart.rows) == 2
    assert not any("autres" in row["label"] for row in chart.rows)


def test_dashboard_shows_the_recruitment_pipeline(client, population, recruteur):
    """Regression : la refonte du tableau de bord avait fait disparaitre les etapes.

    Le bloc affichait « Aucune candidature » alors que la base en comptait
    sept — la cle avait ete perdue en reecrivant la vue pour les graphiques.
    """
    client.force_login(recruteur)
    response = client.get(reverse("candidates:dashboard"))

    stages = response.context["stages"]
    assert stages, "le pipeline ne doit pas etre vide quand des candidatures existent"
    assert sum(row["total"] for row in stages) == 4
    # Les etapes sont affichees en clair, pas sous leur cle technique.
    assert stages[0]["stage"] == "Recue"


def test_ordered_bar_keeps_the_given_order():
    """Les tranches ordonnees ne se retrient pas par effectif."""
    pairs = [("Moins d'un an", 3), ("1 a 3 ans", 9), ("3 a 5 ans", 1)]
    chart = charts.ordered_bar("c", "T", pairs)
    assert [row["label"] for row in chart.rows] == [label for label, _ in pairs]


def test_single_series_has_one_colour_slot():
    """Une serie unique n'est jamais coloree par valeur."""
    chart = charts.bar("c", "T", [("a", 1), ("b", 9)])
    assert len(chart.series) == 1
    assert chart.series[0].slot == 1


def test_grouped_bar_declares_two_slots():
    chart = charts.grouped_bar("c", "T", [("a", 1, 2)], ("Un", "Deux"))
    assert [item.slot for item in chart.series] == [1, 2]
    assert chart.rows[0]["values"] == [1.0, 2.0]


def test_empty_chart_is_detected():
    assert charts.bar("c", "T", []).is_empty
    assert charts.bar("c", "T", [("a", 0), ("b", 0)]).is_empty
    assert not charts.bar("c", "T", [("a", 1)]).is_empty


def test_chart_payload_is_json_serialisable():
    chart = charts.grouped_bar("c", "T", [("a", 1, 2)], ("Un", "Deux"), unit="ms")
    restored = json.loads(json.dumps(chart.as_dict(), ensure_ascii=False))
    assert restored["unit"] == "ms"
    assert restored["kind"] == "bar"
    assert restored["series"][1]["name"] == "Deux"


# --- Jauge circulaire --------------------------------------------------------
def test_ring_computes_its_ratio():
    chart = charts.ring("c", "T", 5, 8)
    assert chart.kind == "ring"
    assert chart.rows[0]["ratio"] == 0.625
    assert chart.rows[0]["total"] == 8.0


def test_ring_survives_a_total_of_zero():
    """Une base vide ne doit pas produire une division par zero."""
    chart = charts.ring("c", "T", 0, 0)
    assert chart.rows[0]["ratio"] == 0.0
    assert chart.is_empty


def test_a_ring_at_zero_is_not_empty():
    """« Zero dossier echu » est precisement le resultat qu'on veut lire."""
    chart = charts.ring("c", "T", 0, 7)
    assert not chart.is_empty
    assert chart.rows[0]["ratio"] == 0.0


def test_ring_carries_its_threshold():
    chart = charts.ring("c", "T", 1, 10, target=0.8, target_label="seuil legal")
    ligne = chart.rows[0]
    assert ligne["target"] == 0.8
    assert ligne["target_label"] == "seuil legal"


def test_ring_declares_a_single_colour_slot():
    """Une seule grandeur, donc une seule couleur et aucune legende."""
    assert len(charts.ring("c", "T", 1, 2).series) == 1


def test_ring_payload_is_json_serialisable():
    chart = charts.ring("c", "T", 3, 4, unit="dossiers", target=0.5, invert=True)
    restored = json.loads(json.dumps(chart.as_dict(), ensure_ascii=False))
    assert restored["kind"] == "ring"
    assert restored["unit"] == "dossiers"
    assert restored["rows"][0]["invert"] is True


def test_the_dashboard_shows_three_gauges(client, population, recruteur):
    client.force_login(recruteur)
    graphiques = client.get(reverse("candidates:dashboard")).context["charts"]

    for cle in ["decided", "shortlist", "retention"]:
        assert graphiques[cle].kind == "ring", cle
        assert graphiques[cle].rows, cle


def test_the_retention_gauge_reads_the_right_way_round(client, population, recruteur):
    """Ici zero est le bon resultat : la jauge le declare, elle ne l'invente pas."""
    client.force_login(recruteur)
    jauge = client.get(reverse("candidates:dashboard")).context["charts"]["retention"]

    assert jauge.rows[0]["invert"] is True
    assert jauge.rows[0]["target"] == 0.0


def test_gauge_values_survive_without_javascript(client, population, recruteur):
    """Le tableau doit porter la part, le tout et la proportion."""
    client.force_login(recruteur)
    contenu = client.get(reverse("candidates:dashboard")).content.decode()

    assert "Proportion" in contenu
    assert "chart-ring-decided" in contenu


@pytest.mark.parametrize(
    ("values", "fraction", "expected"),
    [
        ([], 0.5, 0.0),
        ([7], 0.5, 7.0),
        ([1, 2, 3], 0.5, 2.0),
        ([1, 2, 3, 4], 0.5, 2.5),
        ([1, 2, 3, 4, 5], 0.95, 4.8),
        ([10, 1, 5], 0.0, 1.0),
    ],
)
def test_percentile(values, fraction, expected):
    assert charts.percentile(values, fraction) == pytest.approx(expected)


# --- Donnees metier ----------------------------------------------------------
@pytest.fixture
def population(db):
    offer = JobOffer.objects.create(
        title="Backend", description="x", status=JobOffer.Status.OPEN
    )
    JobSkill.objects.create(offer=offer, name="Python")

    for index, (nom, annees) in enumerate(
        [("Ahmed", 0.5), ("Sara", 2.0), ("Badr", 4.0), ("Leila", 9.0)]
    ):
        candidate = Candidate.objects.create(
            full_name=nom, email=f"{nom.lower()}@example.com",
            total_experience_years=annees,
        )
        CandidateSkill.objects.create(candidate=candidate, name="Python", years=annees)
        if index < 2:
            CandidateSkill.objects.create(candidate=candidate, name="Django")
        CandidateLanguage.objects.create(
            candidate=candidate, language="Francais", level="C1"
        )
        Application.objects.create(candidate=candidate, offer=offer)
    return offer


def test_skills_chart_counts_distinct_candidates(client, population, django_user_model):
    user = django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123"
    )
    client.force_login(user)
    response = client.get(reverse("candidates:dashboard"))

    skills = response.context["charts"]["skills"]
    valeurs = {row["label"]: row["values"][0] for row in skills.rows}
    assert valeurs["python"] == 4
    assert valeurs["django"] == 2


def test_experience_bands_cover_everyone(client, population, django_user_model):
    user = django_user_model.objects.create_user(
        username="rh2", password="mot-de-passe-de-test-123"
    )
    client.force_login(user)
    response = client.get(reverse("candidates:dashboard"))

    chart = response.context["charts"]["experience"]
    assert sum(row["values"][0] for row in chart.rows) == 4
    # Les tranches restent dans leur ordre naturel.
    assert chart.rows[0]["label"].startswith("Moins")


def test_score_chart_counts_each_application_once(
    client, population, django_user_model, monkeypatch
):
    """Un recalcul ne doit pas compter la candidature deux fois."""
    from apps.matching import engine

    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )
    application = Application.objects.first()
    score_application(application, with_explanation=False)
    score_application(application, with_explanation=False)

    user = django_user_model.objects.create_user(
        username="rh3", password="mot-de-passe-de-test-123"
    )
    client.force_login(user)
    response = client.get(reverse("candidates:dashboard"))

    chart = response.context["charts"]["scores"]
    assert sum(row["values"][0] for row in chart.rows) == 1


# --- Rendu -------------------------------------------------------------------
@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="viz", password="mot-de-passe-de-test-123"
    )


def test_values_are_readable_without_javascript(client, population, recruteur):
    """Le tableau equivalent est rendu par le serveur, pas par le navigateur."""
    client.force_login(recruteur)
    content = client.get(reverse("candidates:dashboard")).content.decode()

    assert 'class="chart__table"' in content
    # Les intitules et les valeurs sont dans le HTML livre.
    assert "python" in content
    assert 'type="application/json"' in content


def test_dashboard_renders_every_chart(client, population, recruteur):
    client.force_login(recruteur)
    response = client.get(reverse("candidates:dashboard"))
    assert response.status_code == 200
    assert set(response.context["charts"]) == {
        "decided", "shortlist", "retention",
        "skills", "experience", "languages", "scores",
    }


def test_criterion_meters_are_coloured_by_value(
    client, population, recruteur, monkeypatch
):
    """Regression : toutes les jauges de criteres s'affichaient en rouge.

    `widthratio ... as` produit une chaine ; comparee a un entier dans le
    `{% if %}` de la jauge, la comparaison echoue sans erreur et tout retombe
    sur la branche « danger ». Un critere a 100 % apparaissait donc en rouge.
    """
    from apps.matching import engine

    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )
    application = Application.objects.first()
    score_application(application, with_explanation=False)

    client.force_login(recruteur)
    content = client.get(application.get_absolute_url()).content.decode()

    # Au moins un critere est plein : sa jauge doit etre au palier superieur.
    assert "meter__fill--ok" in content
    assert content.count("meter__fill--danger") < content.count("meter__fill")


def test_invocation_dashboard_requires_login(client, db):
    response = client.get(reverse("evaluation:invocations"))
    assert response.status_code == 302


def test_invocation_dashboard_renders(client, recruteur):
    from apps.ai.models import AIInvocation

    for usage, latence, entree, sortie in [
        ("cv_extraction", 9000, 800, 1100),
        ("cv_extraction", 11000, 820, 1150),
        ("score_explanation", 6000, 500, 830),
    ]:
        AIInvocation.objects.create(
            purpose=usage, model="m", latency_ms=latence,
            prompt_tokens=entree, completion_tokens=sortie,
        )
    AIInvocation.objects.create(
        purpose="cv_extraction", model="m", status=AIInvocation.Status.ERROR,
        error="tronquee",
    )

    client.force_login(recruteur)
    response = client.get(reverse("evaluation:invocations"))

    assert response.status_code == 200
    assert response.context["stats"]["calls"] == 4
    assert response.context["stats"]["failures"] == 1
    assert response.context["stats"]["tokens"] == 800 + 1100 + 820 + 1150 + 500 + 830

    latence = response.context["charts"]["latency"]
    assert [item.name for item in latence.series] == ["Mediane", "95e centile"]
    # Les appels en echec sont exclus des latences : ils fausseraient la mesure.
    extraction = next(row for row in latence.rows if row["label"] == "cv_extraction")
    assert extraction["values"][0] == pytest.approx(10000)


def test_token_chart_separates_input_from_output(client, recruteur):
    from apps.ai.models import AIInvocation

    AIInvocation.objects.create(
        purpose="cv_extraction", model="m", prompt_tokens=800, completion_tokens=1100
    )
    client.force_login(recruteur)
    response = client.get(reverse("evaluation:invocations"))

    tokens = response.context["charts"]["tokens"]
    assert tokens.kind == "stack"
    assert tokens.rows[0]["values"] == [800.0, 1100.0]


def test_empty_dashboards_do_not_break(client, recruteur):
    """Une base vide affiche un etat vide, jamais une erreur."""
    client.force_login(recruteur)
    for nom in ("candidates:dashboard", "evaluation:invocations"):
        response = client.get(reverse(nom))
        assert response.status_code == 200
        assert "Aucune donnee" in response.content.decode()
