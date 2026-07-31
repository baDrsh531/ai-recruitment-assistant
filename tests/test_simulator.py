"""Tests du simulateur de ponderation.

Le simulateur existe pour rendre un arbitrage visible avant qu'il soit
applique. Les tests portent donc sur trois choses : qu'il ne modifie rien, que
le classement simule est bien celui qu'on obtiendrait, et qu'il affiche le prix
de la ponderation en ratio d'impact — c'est ce dernier point qui justifie le
module.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.evaluation import bias
from apps.jobs.models import DEFAULT_WEIGHTS, JobOffer, JobSkill
from apps.matching import engine, simulator


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
    offre = JobOffer.objects.create(
        title="Backend", description="x", status="open",
        experience_min_years=5, location="Casablanca",
    )
    JobSkill.objects.create(
        offer=offre, name="Python", requirement="required", min_years=6
    )

    # Un profil fort en competences et un peu court en anciennete ; l'autre
    # l'inverse. Deux contraintes rendent ce decor moins evident qu'il n'y
    # parait : les deux doivent couvrir la competence obligatoire, faute de quoi
    # le facteur de recevabilite — multiplicatif — ecraserait le second quel que
    # soit le poids donne a l'anciennete ; et « Technique » doit mener sous la
    # ponderation par defaut, sinon aucun basculement n'est observable.
    technique = Candidate.objects.create(
        full_name="Technique", total_experience_years=4, location="Casablanca"
    )
    CandidateSkill.objects.create(
        candidate=technique, name="Python", years=8, last_used_year=2026
    )
    ancien = Candidate.objects.create(
        full_name="Ancien", total_experience_years=6, location="Casablanca"
    )
    CandidateSkill.objects.create(
        candidate=ancien, name="Python", years=3, last_used_year=2024
    )
    for candidat in (technique, ancien):
        Application.objects.create(candidate=candidat, offer=offre)
    return offre


# --- Normalisation -----------------------------------------------------------
def test_weights_are_normalised_to_one():
    poids = simulator.normalise({nom: 2.0 for nom in DEFAULT_WEIGHTS})
    assert round(sum(poids.values()), 4) == 1.0


def test_a_negative_weight_is_clamped():
    """Un poids negatif inverserait le critere : le moteur ne le prevoit pas."""
    poids = simulator.normalise({**DEFAULT_WEIGHTS, "skills": -5})
    assert poids["skills"] == 0.0


def test_an_absurd_weight_is_capped():
    poids = simulator.normalise({**DEFAULT_WEIGHTS, "skills": 99})
    brut = min(99, simulator.POIDS_MAX)
    assert poids["skills"] == pytest.approx(
        brut / (brut + sum(v for k, v in DEFAULT_WEIGHTS.items() if k != "skills")),
        abs=1e-3,
    )


def test_a_non_numeric_weight_falls_back_to_the_default():
    poids = simulator.normalise({**DEFAULT_WEIGHTS, "skills": "beaucoup"})
    assert poids["skills"] > 0


def test_all_weights_at_zero_return_the_default():
    """Tout a zero, le score n'aurait plus de sens : mieux vaut le defaut
    qu'une division par zero deguisee en resultat."""
    poids = simulator.normalise({nom: 0 for nom in DEFAULT_WEIGHTS})
    assert round(sum(poids.values()), 4) == 1.0
    assert poids["skills"] > 0


def test_an_unknown_key_is_ignored():
    poids = simulator.normalise({**DEFAULT_WEIGHTS, "karma": 5})
    assert "karma" not in poids


# --- Effet sur le classement -------------------------------------------------
def test_weighting_seniority_reorders_the_ranking(offre):
    par_defaut = simulator.simulate(offre, DEFAULT_WEIGHTS, with_bias=False)
    premier = par_defaut.rows[0].application.candidate.full_name

    anciennete = simulator.simulate(
        offre, {**DEFAULT_WEIGHTS, "skills": 0.05, "experience": 0.80},
        with_bias=False,
    )

    assert premier == "Technique"
    assert anciennete.rows[0].application.candidate.full_name == "Ancien"
    assert anciennete.mouvements == 2


def test_the_default_weighting_moves_nobody(offre):
    simulation = simulator.simulate(offre, offre.weights, with_bias=False)
    assert simulation.mouvements == 0
    assert all(not ligne.a_bouge for ligne in simulation.rows)


def test_rows_are_ordered_by_the_simulated_rank(offre):
    simulation = simulator.simulate(
        offre, {**DEFAULT_WEIGHTS, "experience": 0.80}, with_bias=False
    )
    rangs = [ligne.rang_simule for ligne in simulation.rows]
    assert rangs == sorted(rangs)


def test_a_rise_is_a_positive_delta(offre):
    simulation = simulator.simulate(
        offre, {**DEFAULT_WEIGHTS, "skills": 0.05, "experience": 0.80},
        with_bias=False,
    )
    monte = next(
        ligne for ligne in simulation.rows
        if ligne.application.candidate.full_name == "Ancien"
    )
    assert monte.delta_rang > 0


# --- Rien n'est enregistre ---------------------------------------------------
def test_the_offer_keeps_its_own_weights(offre):
    """Une simulation interrompue ne doit pas laisser des poids etrangers."""
    avant = dict(offre.weights)

    simulator.simulate(offre, {**DEFAULT_WEIGHTS, "skills": 0.05}, with_bias=False)
    offre.refresh_from_db()

    assert offre.weights == avant
    assert offre.scoring_weights == {}


def test_no_score_is_written(offre):
    from apps.matching.models import MatchScore

    avant = MatchScore.objects.count()
    simulator.simulate(offre, {**DEFAULT_WEIGHTS, "skills": 0.05}, with_bias=False)
    assert MatchScore.objects.count() == avant


# --- Le prix de la ponderation -----------------------------------------------
def test_the_engine_accepts_a_weighting_without_touching_the_offer(offre):
    candidat = Candidate.objects.get(full_name="Technique")

    normal = engine.score(candidat, offre).overall
    force = engine.score(
        candidat, offre, weights=simulator.normalise({**DEFAULT_WEIGHTS, "experience": 0.9})
    ).overall

    assert normal != force
    offre.refresh_from_db()
    assert offre.scoring_weights == {}


def test_lowering_the_skills_weight_degrades_the_impact_ratio(db):
    """Le resultat qui justifie ce module.

    Le poids de la localisation ne bouge pas, et le ratio tombe pourtant sous
    le seuil legal : quand les competences cessent de departager, ce sont les
    criteres restants qui decident, localisation comprise. Aucun recruteur ne
    devinerait cela en deplacant un curseur.
    """
    defaut = simulator.normalise(DEFAULT_WEIGHTS)
    affaibli = simulator.normalise(
        {**DEFAULT_WEIGHTS, "skills": 0.20, "experience": 0.45}
    )

    assert affaibli["location"] == defaut["location"], "la localisation ne bouge pas"

    ratio_defaut = bias.impact_ratio_for_weights(defaut)
    ratio_affaibli = bias.impact_ratio_for_weights(affaibli)

    assert ratio_defaut >= bias.IMPACT_RATIO_THRESHOLD
    assert ratio_affaibli < ratio_defaut
    assert ratio_affaibli < bias.IMPACT_RATIO_THRESHOLD


def test_removing_location_neutralises_the_ratio(db):
    poids = simulator.normalise({**DEFAULT_WEIGHTS, "location": 0.0})
    assert bias.impact_ratio_for_weights(poids) == 1.0


def test_an_unknown_dimension_is_refused(db):
    with pytest.raises(ValueError, match="Dimension inconnue"):
        bias.impact_ratio_for_weights(DEFAULT_WEIGHTS, dimension="astrologie")


def test_a_degraded_ratio_is_declared(offre):
    simulation = simulator.simulate(
        offre, {**DEFAULT_WEIGHTS, "skills": 0.20, "experience": 0.45}
    )
    assert simulation.degrade_le_ratio
    assert simulation.impact_delta < 0


# --- Interface ---------------------------------------------------------------
def test_the_page_renders_with_the_current_weighting(client, offre, recruteur):
    client.force_login(recruteur)
    reponse = client.get(reverse("matching:simulator", kwargs={"slug": offre.slug}))

    assert reponse.status_code == 200
    assert reponse.context["modified"] is False
    assert reponse.context["simulation"].mouvements == 0


def test_the_form_carries_the_current_weights(client, offre, recruteur):
    """Regression : le formulaire perdait les poids courants, en silence.

    Deux causes independantes, l'une et l'autre suffisantes. La locale
    francaise rend 0.45 en « 0,45 » et un champ `type="number"` refuse la
    virgule ; et un pas de 0,01 rend invalide une valeur normalisee comme
    0.0417. Dans les deux cas le navigateur affiche un champ vide sans
    qu'aucune erreur ne remonte.
    """
    client.force_login(recruteur)
    reponse = client.get(reverse("matching:simulator", kwargs={"slug": offre.slug}))
    contenu = reponse.content.decode()

    assert 'step="any"' in contenu
    assert 'value="0,' not in contenu, "une virgule decimale invalide le champ"
    for valeur in reponse.context["weights"].values():
        assert f'value="{valeur}"' in contenu


def test_the_page_accepts_weights_from_the_query(client, offre, recruteur):
    client.force_login(recruteur)
    reponse = client.get(
        reverse("matching:simulator", kwargs={"slug": offre.slug}),
        {"poids_skills": "0.05", "poids_experience": "0.80"},
    )

    assert reponse.context["modified"] is True
    assert reponse.context["simulation"].mouvements > 0


def test_the_page_warns_when_the_legal_threshold_is_crossed(client, offre, recruteur):
    client.force_login(recruteur)
    reponse = client.get(
        reverse("matching:simulator", kwargs={"slug": offre.slug}),
        {"poids_skills": "0.20", "poids_experience": "0.45"},
    )

    contenu = reponse.content.decode()
    assert reponse.context["simulation"].impact_ratio < bias.IMPACT_RATIO_THRESHOLD
    assert "sous le seuil legal" in contenu
    assert "quatre cinquiemes" in contenu


def test_a_viewer_may_simulate(client, offre, db, django_user_model):
    """La page ne modifie rien : c'est une lecture du systeme."""
    observateur = django_user_model.objects.create_user(
        username="obs", password="mot-de-passe-de-test-123", role="viewer"
    )
    client.force_login(observateur)
    reponse = client.get(reverse("matching:simulator", kwargs={"slug": offre.slug}))
    assert reponse.status_code == 200


def test_the_simulation_serialises(offre):
    donnees = simulator.simulate(offre, DEFAULT_WEIGHTS, with_bias=False).as_dict()
    assert set(donnees) >= {"weights", "movements", "rows"}
    assert donnees["rows"][0]["rank_after"] == 1
