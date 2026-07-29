"""Tests de l'analyse contrefactuelle.

Un score seul n'est pas actionnable. Ces tests verifient que le rapport dit
vrai : que les gains annonces correspondent a ce que le moteur produit
reellement une fois le changement applique, que le chemin est le plus court
trouve, et surtout qu'un ecart infranchissable est annonce comme tel plutot que
deguise en liste d'efforts.
"""

from __future__ import annotations

import pytest

from apps.candidates.models import Candidate, CandidateLanguage, CandidateSkill
from apps.jobs.models import EducationLevel, JobLanguage, JobOffer, JobSkill, LanguageLevel
from apps.matching import counterfactual, engine


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


def _offre(**surcharges) -> JobOffer:
    defauts = {
        "title": "Backend Python",
        "description": "x",
        "status": "open",
        "experience_min_years": 4,
        "education_level": EducationLevel.MASTER,
    }
    offre = JobOffer.objects.create(**{**defauts, **surcharges})
    JobSkill.objects.create(offer=offre, name="Python", requirement="required", min_years=2)
    JobSkill.objects.create(offer=offre, name="Django", requirement="required", min_years=2)
    JobSkill.objects.create(offer=offre, name="Docker", requirement="preferred")
    return offre


def _candidat(nom="Alice", annees=2.0, diplome=EducationLevel.BACHELOR, competences=None):
    candidat = Candidate.objects.create(
        full_name=nom, total_experience_years=annees, highest_education=diplome
    )
    for name, years in competences or []:
        CandidateSkill.objects.create(
            candidate=candidat, name=name, years=years, last_used_year=2026
        )
    return candidat


# --- Fidelite du rapport -----------------------------------------------------
def test_the_announced_gain_matches_what_the_engine_produces(db):
    """Un gain annonce qui ne se verifie pas serait pire qu'aucun gain."""
    offre = _offre()
    candidat = _candidat(competences=[("Python", 3)])

    rapport = counterfactual.analyse(candidat, offre)
    levier = next(lever for lever in rapport.levers if lever.label == "Django")

    # On applique reellement le changement, puis on rejoue le moteur.
    CandidateSkill.objects.create(
        candidate=candidat, name="Django", years=2, last_used_year=2026
    )
    candidat.refresh_from_db()
    reel = engine.score(candidat, offre).overall

    assert round(reel, 4) == round(levier.score_if_applied, 4)


def test_the_simulation_writes_nothing(db):
    offre = _offre()
    candidat = _candidat(competences=[("Python", 3)])
    avant = CandidateSkill.objects.count()

    counterfactual.analyse(candidat, offre)

    assert CandidateSkill.objects.count() == avant
    assert list(candidat.skills.values_list("name", flat=True)) == ["Python"]
    candidat.refresh_from_db()
    assert candidat.total_experience_years == 2.0
    assert candidat.highest_education == EducationLevel.BACHELOR


def test_gains_are_not_additive_and_the_path_says_so(db):
    """Le facteur de recevabilite est multiplicatif : additionner mentirait."""
    offre = _offre()
    candidat = _candidat(annees=1.0, competences=[("Python", 1)])

    # Un seuil pris juste sous le plafond garantit un chemin de plusieurs pas,
    # sans dependre d'une constante devinee a l'avance.
    plafond = counterfactual.analyse(candidat, offre, target=2.0).ceiling
    rapport = counterfactual.analyse(candidat, offre, target=plafond - 0.01)

    assert len(rapport.path) >= 2

    # Les apports marginaux, eux, s'additionnent : c'est ce qui rend le tableau
    # lisible.
    somme_marginale = rapport.current + sum(step.gain for step in rapport.path)
    assert abs(somme_marginale - rapport.path[-1].cumulative_score) < 1e-9

    # Les apports isoles, non : au moins un levier rapporte moins une fois les
    # autres appliques. C'est la trace du facteur de recevabilite.
    somme_isolee = rapport.current + sum(step.standalone_gain for step in rapport.path)
    assert abs(somme_isolee - rapport.path[-1].cumulative_score) > 1e-6, (
        "le cumul du chemin doit venir d'un recalcul, pas d'une addition"
    )


def test_the_cumulative_score_only_grows(db):
    offre = _offre()
    candidat = _candidat(competences=[("Python", 1)])
    rapport = counterfactual.analyse(candidat, offre)

    scores = [rapport.current] + [step.cumulative_score for step in rapport.path]
    assert scores == sorted(scores)


# --- Contenu des leviers -----------------------------------------------------
def test_a_missing_required_skill_becomes_a_lever(db):
    offre = _offre()
    candidat = _candidat(competences=[("Python", 3)])

    rapport = counterfactual.analyse(candidat, offre)
    competences = {lever.label for lever in rapport.levers if lever.kind == "skill"}

    assert "Django" in competences
    assert "Python" not in competences, "une competence deja couverte n'est pas un levier"


def test_missing_seniority_and_degree_become_levers(db):
    offre = _offre()
    candidat = _candidat(annees=1.0, diplome=EducationLevel.HIGH_SCHOOL,
                         competences=[("Python", 3), ("Django", 3)])

    rapport = counterfactual.analyse(candidat, offre)
    types = {lever.kind for lever in rapport.levers}

    assert "experience" in types
    assert "education" in types
    experience = next(lever for lever in rapport.levers if lever.kind == "experience")
    assert "3.0 an(s) manquant(s)" in experience.effort


def test_a_missing_language_becomes_a_lever(db):
    offre = _offre()
    JobLanguage.objects.create(offer=offre, language="Anglais", min_level=LanguageLevel.B2)
    candidat = _candidat(competences=[("Python", 3), ("Django", 3)])
    CandidateLanguage.objects.create(
        candidate=candidat, language="Francais", level=LanguageLevel.NATIVE
    )

    rapport = counterfactual.analyse(candidat, offre)
    langues = [lever for lever in rapport.levers if lever.kind == "language"]

    assert [lever.label for lever in langues] == ["Anglais"]
    assert "absente du profil" in langues[0].effort


def test_a_satisfied_language_is_not_a_lever(db):
    offre = _offre()
    JobLanguage.objects.create(offer=offre, language="Anglais", min_level=LanguageLevel.B2)
    candidat = _candidat(competences=[("Python", 3), ("Django", 3)])
    CandidateLanguage.objects.create(
        candidate=candidat, language="Anglais", level=LanguageLevel.C1
    )

    rapport = counterfactual.analyse(candidat, offre)
    assert not [lever for lever in rapport.levers if lever.kind == "language"]


def test_levers_are_ordered_by_what_they_bring(db):
    offre = _offre()
    candidat = _candidat(annees=1.0, competences=[("Python", 1)])

    rapport = counterfactual.analyse(candidat, offre)
    gains = [lever.gain for lever in rapport.levers]

    assert gains == sorted(gains, reverse=True)
    assert all(gain > 0 for gain in gains), "un levier sans gain n'est pas un levier"


# --- Chemin et plafond -------------------------------------------------------
def test_a_candidate_already_above_the_threshold_gets_no_path(db):
    offre = _offre(experience_min_years=1, education_level=EducationLevel.NONE)
    candidat = _candidat(annees=6.0, diplome=EducationLevel.PHD,
                         competences=[("Python", 6), ("Django", 6), ("Docker", 4)])

    rapport = counterfactual.analyse(candidat, offre)

    assert rapport.already_there
    assert rapport.reached
    assert rapport.path == []


def test_the_path_stops_as_soon_as_the_threshold_is_reached(db):
    offre = _offre()
    candidat = _candidat(annees=1.0, competences=[("Python", 1)])

    depart = counterfactual.analyse(candidat, offre, target=2.0)
    seuil = (depart.current + depart.ceiling) / 2
    rapport = counterfactual.analyse(candidat, offre, target=seuil)

    assert not rapport.already_there
    assert rapport.reached
    assert rapport.path[-1].cumulative_score >= seuil
    # Aucun changement de trop : le pas precedent restait sous le seuil.
    for etape in rapport.path[:-1]:
        assert etape.cumulative_score < seuil


def test_an_unreachable_target_is_announced_as_such(db):
    """Mieux vaut dire « hors de portee » que produire une liste de courses.

    Le cas est construit sur la localisation, et ce n'est pas un hasard : c'est
    le seul critere que le module refuse deliberement de traiter en levier.
    « Demenagez » n'est pas un conseil qu'un outil de recrutement a a donner, et
    c'est justement le critere que l'audit de biais a identifie comme porteur
    d'un signal identitaire.
    """
    offre = _offre(location="Casablanca", remote_policy="onsite")
    candidat = _candidat(competences=[])
    candidat.location = "Tanger"
    candidat.save()

    rapport = counterfactual.analyse(candidat, offre, target=0.99)

    assert not rapport.reached
    assert rapport.ceiling < 0.99
    assert all(lever.kind != "location" for lever in rapport.levers)


def test_a_required_certification_becomes_a_lever(db):
    offre = _offre(required_certifications=["AWS Solutions Architect"])
    candidat = _candidat(competences=[("Python", 3), ("Django", 3)])

    rapport = counterfactual.analyse(candidat, offre)
    certifications = [lever for lever in rapport.levers if lever.kind == "certification"]

    assert [lever.label for lever in certifications] == ["AWS Solutions Architect"]


def test_the_ceiling_is_the_score_with_every_lever_applied(db):
    offre = _offre()
    candidat = _candidat(annees=1.0, competences=[("Python", 1)])

    rapport = counterfactual.analyse(candidat, offre, target=2.0)

    assert rapport.ceiling >= rapport.current
    # Le chemin ne peut pas depasser le plafond : c'est la meme grandeur.
    if rapport.path:
        assert rapport.path[-1].cumulative_score <= rapport.ceiling + 1e-9


def test_the_path_is_bounded(db):
    offre = _offre()
    for index in range(12):
        JobSkill.objects.create(
            offer=offre, name=f"Techno{index}", requirement="required", min_years=1
        )
    candidat = _candidat(competences=[])

    rapport = counterfactual.analyse(candidat, offre, target=1.0)
    assert len(rapport.path) <= counterfactual.MAX_STEPS


# --- Coherence avec le reste du systeme --------------------------------------
def test_blind_screening_is_respected(db):
    """Le contrefactuel ne doit pas reintroduire un critere que l'offre exclut."""
    offre = _offre(location="Casablanca", blind_screening=True)
    candidat = _candidat(competences=[("Python", 3)])
    candidat.location = "Tanger"
    candidat.save()

    rapport = counterfactual.analyse(candidat, offre)
    assert all(lever.kind != "location" for lever in rapport.levers)
    assert rapport.current == engine.score(candidat, offre, blind=True).overall


def test_the_starting_point_is_the_engine_score(db):
    offre = _offre()
    candidat = _candidat(competences=[("Python", 3)])

    rapport = counterfactual.analyse(candidat, offre)
    assert rapport.current == engine.score(candidat, offre).overall


def test_the_report_states_whether_the_profile_came_from_a_cv(db):
    """Une competence absente peut n'etre qu'une competence non extraite."""
    offre = _offre()
    candidat = _candidat(competences=[("Python", 3)])

    rapport = counterfactual.analyse(candidat, offre)
    assert rapport.extracted_from_cv is False


def test_the_report_serialises(db):
    offre = _offre()
    candidat = _candidat(competences=[("Python", 3)])

    donnees = counterfactual.analyse(candidat, offre).as_dict()

    assert set(donnees) >= {"current", "target", "reached", "ceiling", "levers", "path"}
    if donnees["path"]:
        assert {"cumulative_percentage", "standalone_gain_points"} <= set(donnees["path"][0])


def test_the_table_adds_up(db):
    """Un tableau dont les lignes ne s'additionnent pas se lit comme un bug."""
    offre = _offre()
    candidat = _candidat(annees=1.0, competences=[])

    rapport = counterfactual.analyse(candidat, offre)
    courant = rapport.current
    for etape in rapport.path:
        assert abs(courant + etape.gain - etape.cumulative_score) < 1e-9
        courant = etape.cumulative_score
