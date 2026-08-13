"""CV distincts au contenu commun, et variance du modele.

Deux mesures qui se ressemblent par la forme — elles comparent des textes — et
que rien d'autre ne rapproche.

La premiere cherche ce que deux candidats ont copie l'un sur l'autre. Son
piege est le tout-venant : « experience professionnelle », « permis B » se
retrouvent partout, et une mesure naive rapproche tout le monde de tout le
monde. Ces tests eprouvent surtout ce qu'elle refuse de signaler.

La seconde verifie l'affirmation centrale du projet : le modele de langage
commente un chiffre, il n'en produit aucun. Le test qui compte est celui du
chiffre invente.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.candidates import plagiarism
from apps.candidates.models import Application, Candidate, CVDocument
from apps.evaluation import variance
from apps.jobs.models import JobOffer, JobSkill
from apps.matching.models import MatchScore

# Un texte assez long pour depasser le minimum d'empreintes.
SOCLE = (
    "Ingenieur logiciel specialise dans la conception de plateformes de "
    "donnees a grande echelle. J'ai conduit la migration du socle applicatif "
    "vers une architecture orientee services, en pilotant une equipe de six "
    "personnes reparties sur deux sites. Mon travail a porte sur la fiabilite "
    "des traitements nocturnes et sur la reduction du temps de restitution des "
    "tableaux de bord commerciaux. J'interviens aussi sur la formation des "
    "nouveaux arrivants et sur la revue des choix techniques structurants. "
)
AUTRE = (
    "Chargee de mission en developpement territorial, je conduis des projets "
    "de revitalisation de centres-villes en lien avec les collectivites. Mon "
    "quotidien mele animation de reunions publiques, montage de dossiers de "
    "subvention et suivi de chantiers avec les maitres d'oeuvre. J'ai pilote "
    "la renovation de trois halles de marche et l'ouverture d'une maison de "
    "services au public. Je forme egalement les agents aux outils de suivi "
    "budgetaire et j'anime le reseau des referents de quartier. "
)
BANALITES = (
    "Experience professionnelle. Formation. Langues francais anglais. "
    "Permis B. Centres d'interet lecture sport voyages. References "
    "disponibles sur demande. "
)


def _cv(nom: str, texte: str, *, candidat=None) -> CVDocument:
    import hashlib

    if candidat is None:
        candidat = Candidate.objects.create(full_name=nom)
    return CVDocument.objects.create(
        candidate=candidat,
        original_filename=f"{nom}.pdf",
        content_hash=hashlib.sha256(texte.encode() + nom.encode()).hexdigest(),
        status=CVDocument.Status.DONE,
        raw_text=texte,
    )


# --- Ce que la mesure signale ------------------------------------------------
def test_a_copied_cv_is_flagged(db):
    _cv("Alice", SOCLE)
    _cv("Bob", SOCLE)

    rapport = plagiarism.analyser()

    assert len(rapport.paires) == 1
    assert rapport.paires[0].similarite > 0.9
    assert rapport.paires[0].gravite == "quasi identique"


def test_a_lightly_edited_copy_is_still_flagged(db):
    """Un CV recopie puis passe au correcteur ne se trahit plus par sa casse ni
    sa ponctuation."""
    _cv("Alice", SOCLE)
    _cv("Bob", SOCLE.upper().replace(",", ";").replace(".", " !"))

    rapport = plagiarism.analyser()

    assert rapport.paires, "la normalisation doit absorber casse et ponctuation"


# --- Ce que la mesure refuse de signaler -------------------------------------
def test_two_unrelated_cvs_are_not_flagged(db):
    _cv("Alice", SOCLE)
    _cv("Bob", AUTRE)

    assert plagiarism.analyser().paires == []


def test_shared_boilerplate_alone_does_not_flag_anyone(db):
    """« Experience professionnelle », « permis B » se retrouvent dans un CV
    sur deux. Sans garde-fou, deux CV sans rapport se rejoignent dessus."""
    for index in range(6):
        texte = (SOCLE if index % 2 else AUTRE) + BANALITES
        _cv(f"Candidat {index}", texte + f" Detail propre numero {index}. " * 4)

    rapport = plagiarism.analyser()

    assert rapport.empreintes_retirees > 0, "le tout-venant doit etre retire"
    for paire in rapport.paires:
        assert paire.similarite < 0.99


def test_two_cvs_of_the_same_candidate_are_left_to_the_duplicates_page(db):
    """Les melanger noierait le signal utile."""
    candidat = Candidate.objects.create(full_name="Sara")
    _cv("v1", SOCLE, candidat=candidat)
    _cv("v2", SOCLE, candidat=candidat)

    assert plagiarism.analyser().paires == []


def test_a_text_too_short_to_mean_anything_is_set_aside(db):
    _cv("Court", "Ingenieur. Python. Paris.")
    _cv("Long", SOCLE)

    rapport = plagiarism.analyser()

    assert rapport.documents_ignores == 1
    assert rapport.documents_compares == 1
    assert "rien a comparer" in rapport.lecture


def test_a_failed_extraction_is_never_compared(db):
    _cv("Alice", SOCLE)
    rate = _cv("Rate", SOCLE)
    CVDocument.objects.filter(pk=rate.pk).update(status=CVDocument.Status.FAILED)

    assert plagiarism.analyser().paires == []


# --- Les briques -------------------------------------------------------------
def test_fingerprints_need_a_whole_shared_sentence():
    """Huit mots ne se retrouvent identiques que si deux textes partagent une
    phrase entiere, ce qui est un fait et non un hasard."""
    assert plagiarism.TAILLE_EMPREINTE >= 8

    court = plagiarism.empreintes("nous avons mis en place")
    assert court == set(), "une formule courte ne produit aucune empreinte"


def test_normalisation_drops_accents_case_and_punctuation():
    assert plagiarism.normaliser("Élève, à Paris !") == ["eleve", "a", "paris"]


@pytest.mark.parametrize("gauche,droite,attendu", [
    (set(), set(), 0.0),
    ({"a"}, set(), 0.0),
    ({"a", "b"}, {"a", "b"}, 1.0),
    ({"a", "b"}, {"b", "c"}, 1 / 3),
])
def test_jaccard(gauche, droite, attendu):
    assert plagiarism.jaccard(gauche, droite) == pytest.approx(attendu)


def test_the_reporting_threshold_stays_high(db):
    """Un faux positif porte ici une accusation ; un faux negatif ne fait rien
    perdre. Le seuil penche donc du cote prudent."""
    assert plagiarism.SEUIL_SIGNALEMENT >= 0.30


def test_the_command_runs(db):
    _cv("Alice", SOCLE)
    _cv("Bob", SOCLE)
    call_command("check_plagiarism")


def test_the_page_renders(db, client, django_user_model):
    _cv("Alice", SOCLE)
    _cv("Bob", SOCLE)
    recruteur = django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )
    client.force_login(recruteur)

    contenu = client.get("/candidats/cv-similaires/").content.decode()

    assert "Paires signalees" in contenu
    assert "quasi identique" in contenu


def test_the_page_hides_names_in_blind_screening(db, client, django_user_model):
    _cv("Alice Martin", SOCLE)
    _cv("Bob Durand", SOCLE)
    aveugle = django_user_model.objects.create_user(
        username="aveugle", password="mot-de-passe-de-test-123",
        role="recruiter", blind_screening=True,
    )
    client.force_login(aveugle)

    contenu = client.get("/candidats/cv-similaires/").content.decode()

    assert "Alice Martin" not in contenu
    assert "Bob Durand" not in contenu
    assert "Identites masquees" in contenu
    # Le nom du fichier aussi : un CV s'appelle presque toujours du nom de son
    # auteur, et l'afficher rendrait le masquage decoratif.
    assert ".pdf" not in contenu


def test_the_documents_page_hides_names_and_filenames_in_blind_screening(
    db, client, django_user_model
):
    """Cette page affichait le nom du candidat et le nom du fichier sans tenir
    compte du screening a l'aveugle. L'attenuation du biais etait annulee par
    la liste des depots, une page en apparence anodine."""
    _cv("Alice Martin", SOCLE)
    aveugle = django_user_model.objects.create_user(
        username="aveugle2", password="mot-de-passe-de-test-123",
        role="recruiter", blind_screening=True,
    )
    client.force_login(aveugle)

    contenu = client.get("/cv/documents/").content.decode()

    assert "Alice Martin" not in contenu
    assert "nom de fichier masque" in contenu


# --- Variance du modele ------------------------------------------------------
@pytest.fixture
def dossier_score(db):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    candidat = Candidate.objects.create(full_name="Alice", total_experience_years=5)
    candidature = Application.objects.create(candidate=candidat, offer=offre)
    MatchScore.objects.create(
        application=candidature,
        overall=0.68,
        engine_version="1.2.0",
        weights_used={"skills": 0.5},
        breakdown={"criteria": [{"name": "skills", "score": 0.72, "applicable": True}]},
        skill_matches=[{"required": "Python", "score": 0.9}],
    )
    return candidature


def _tirages(mesure, *textes):
    mesure.tirages = [variance.Tirage(texte=texte) for texte in textes]
    return mesure


def test_a_quoted_figure_absent_from_the_score_is_caught(dossier_score):
    """La seule faute grave que ce module puisse reveler : un chiffre faux
    presente avec l'autorite d'un chiffre calcule."""
    mesure = variance.Mesure(score=0.68, chiffres_attendus={68, 72, 90, 50})
    _tirages(mesure, "Le candidat obtient 72 % sur les competences.",
             "Un score de 41 % sur les competences.")

    assert mesure.chiffres_inventes == [(2, 41)]
    assert not mesure.fidele
    assert "ne correspondent a aucun chiffre" in mesure.lecture


def test_a_rounded_figure_is_not_an_invention(dossier_score):
    """Le modele est prie d'arrondir, pas d'inventer : un point d'ecart vient
    d'un arrondi, dix d'une invention."""
    mesure = variance.Mesure(score=0.68, chiffres_attendus={68})
    _tirages(mesure, "Un score de 69 %.")

    assert mesure.fidele


def test_the_score_never_moves_between_two_runs():
    """Propriete structurelle, pas statistique : le score n'est pas recalcule,
    il est passe en entree au modele."""
    mesure = variance.Mesure(score=0.68)
    _tirages(mesure, "Un texte.", "Un autre texte.")

    assert mesure.score_stable


def test_the_wording_variation_is_measured():
    mesure = variance.Mesure(score=0.68)
    _tirages(
        mesure,
        "Le profil couvre les attendus techniques du poste propose.",
        "Le profil couvre les attendus techniques du poste propose.",
    )
    assert mesure.recouvrement_median == 1.0

    autre = variance.Mesure(score=0.68)
    _tirages(autre, "Alpha beta gamma delta.", "Epsilon zeta eta theta.")
    assert autre.recouvrement_median == 0.0


def test_a_single_run_cannot_measure_variance():
    mesure = variance.Mesure(score=0.68)
    _tirages(mesure, "Un seul texte.")

    assert mesure.recouvrement_median is None
    assert mesure.ecart_de_longueur is None
    assert "rien a comparer" in mesure.lecture


def test_an_unreachable_model_is_reported_not_hidden(dossier_score, monkeypatch):
    """Le score reste affichable : c'est precisement ce que l'architecture
    garantit."""
    from apps.matching import explain

    monkeypatch.setattr(explain, "explain", lambda *a, **k: {})

    mesure = variance.mesurer(dossier_score, tirages=2)

    assert mesure.indisponible
    assert mesure.tirages == []


def test_the_expected_figures_come_from_the_score_detail(dossier_score):
    score = dossier_score.scores.first()

    attendus = variance._chiffres_du_score(score)

    assert 68 in attendus, "le score global"
    assert 72 in attendus, "un critere du detail"
    assert 90 in attendus, "un rapprochement de competence"
    assert 50 in attendus, "un poids"


def test_the_variance_command_reports_an_unreachable_model(
    dossier_score, monkeypatch, capsys
):
    from apps.matching import explain

    monkeypatch.setattr(explain, "explain", lambda *a, **k: {})

    call_command("measure_variance", "--candidature", str(dossier_score.pk))

    assert "n'a rien renvoye" in capsys.readouterr().out


def test_the_variance_page_never_calls_the_model_on_load(
    dossier_score, client, django_user_model, monkeypatch
):
    """Une page qui mesurerait a chaque visite serait une facture qui court
    toute seule."""
    appels = []
    monkeypatch.setattr(
        variance, "mesurer", lambda *a, **k: appels.append(1) or variance.Mesure(0.0)
    )
    recruteur = django_user_model.objects.create_user(
        username="rh2", password="mot-de-passe-de-test-123", role="recruiter"
    )
    client.force_login(recruteur)

    reponse = client.get("/transparence/variance/")

    assert reponse.status_code == 200
    assert appels == [], "aucun appel au modele au chargement"
