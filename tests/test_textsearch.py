"""Tests de la recherche plein texte.

Ce qui est verifie ici n'est pas « la recherche trouve », mais qu'elle trouve
*pour les bonnes raisons* : qu'un terme rare pese plus qu'un terme banal, qu'un
document long ne l'emporte pas par sa seule longueur, et qu'une requete sans
reponse renvoie une liste vide au lieu de remplir l'ecran.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.assistant import textsearch
from apps.candidates.models import Candidate, CandidateSkill, Experience
from apps.evaluation import search_eval


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


def _candidat(nom, *, headline="", competences=(), experiences=()):
    candidat = Candidate.objects.create(full_name=nom, headline=headline)
    for competence in competences:
        CandidateSkill.objects.create(candidate=candidat, name=competence)
    for titre, entreprise, description in experiences:
        Experience.objects.create(
            candidate=candidat, title=titre, company=entreprise, description=description
        )
    return candidat


# --- Decoupage ---------------------------------------------------------------
def test_tokenising_ignores_case_and_accents():
    assert textsearch.tokenise("Sécurité Applicative") == ["securite", "applicative"]


def test_tokenising_keeps_digits_and_technical_symbols():
    """« ISO 27001 » et « C++ » perdent leur sens sans leurs caracteres."""
    jetons = textsearch.tokenise("ISO 27001, C++ et 3-D Secure")
    assert "27001" in jetons
    assert "c++" in jetons
    # « 3-D » survit comme compose : pris separement, ses deux moitiés
    # tomberaient sous la longueur minimale et le terme disparaitrait.
    assert "3-d" in jetons


def test_a_compound_term_is_indexed_whole_and_in_pieces():
    """Sinon « bout en bout » ne trouverait jamais « bout-en-bout »."""
    jetons = textsearch.tokenise("tests bout-en-bout")
    assert "bout-en-bout" in jetons
    assert "bout" in jetons


def test_tokenising_drops_words_that_separate_nobody():
    jetons = textsearch.tokenise("le developpement de la plateforme avec des outils")
    assert "de" not in jetons and "la" not in jetons and "avec" not in jetons
    assert "developpement" in jetons and "plateforme" in jetons


# --- Comportement BM25 -------------------------------------------------------
def test_a_rare_term_outranks_a_common_one(db):
    """C'est toute la difference avec une recherche par sous-chaine."""
    rare = _candidat(
        "Rare", experiences=[("Ingenieur", "Fintech", "Integration SEPA et virements")]
    )
    for index in range(5):
        _candidat(
            f"Commun {index}",
            experiences=[("Ingenieur", "Societe", "Gestion de virements internes")],
        )

    resultat = textsearch.search("SEPA virements")
    assert resultat.hits[0].candidate.pk == rare.pk


def test_a_long_profile_does_not_win_by_length_alone(db):
    """Sans normalisation par la longueur, le bavard gagnerait toujours."""
    concis = _candidat(
        "Concis", competences=["Kubernetes"],
        experiences=[("Ingenieur plateforme", "Telecom", "Clusters Kubernetes")],
    )
    _candidat(
        "Bavard",
        experiences=[(
            "Ingenieur", "Groupe",
            "Kubernetes " + " ".join(f"activite{index}" for index in range(300)),
        )],
    )

    resultat = textsearch.search("Kubernetes")
    assert resultat.hits[0].candidate.pk == concis.pk


def test_repeating_a_term_saturates(db):
    """Vingt occurrences ne valent pas vingt fois une occurrence."""
    une_fois = _candidat("Une fois", experiences=[("Dev", "A", "paiement")])
    vingt_fois = _candidat("Vingt fois", experiences=[("Dev", "B", "paiement " * 20)])

    resultat = textsearch.search("paiement")
    scores = {hit.candidate.pk: hit.score for hit in resultat.hits}
    assert scores[vingt_fois.pk] > scores[une_fois.pk]
    assert scores[vingt_fois.pk] < 20 * scores[une_fois.pk]


def test_a_query_with_no_answer_returns_nothing(db):
    _candidat("Backend", competences=["Python", "Django"])
    resultat = textsearch.search("pilotage d'eoliennes offshore")

    assert resultat.hits == []
    assert resultat.count == 0


def test_an_empty_query_returns_nothing(db):
    _candidat("Backend", competences=["Python"])
    assert textsearch.search("   ").hits == []


def test_the_search_looks_beyond_declared_skills(db):
    """« systemes de paiement » n'est la competence de personne."""
    cible = _candidat(
        "Paiement", competences=["Python"],
        experiences=[("Ingenieur", "Fintech", "Integration de systemes de paiement")],
    )
    _candidat("Autre", competences=["Python"], experiences=[("Dev", "X", "APIs internes")])

    resultat = textsearch.search("systemes de paiement")
    assert [hit.candidate.pk for hit in resultat.hits] == [cible.pk]


def test_matched_terms_are_reported(db):
    """Un resultat sans justification serait une boite noire de plus."""
    _candidat("Cible", experiences=[("Ingenieur", "Fintech", "Integration SEPA")])
    hit = textsearch.search("SEPA paiement").hits[0]

    assert hit.matched_terms == ["sepa"]


def test_results_are_ranked_and_numbered(db):
    for index in range(3):
        _candidat(f"Profil {index}", experiences=[("Dev", "A", "paiement " * (index + 1))])

    hits = textsearch.search("paiement").hits
    assert [hit.rank for hit in hits] == [1, 2, 3]
    assert [hit.score for hit in hits] == sorted(
        [hit.score for hit in hits], reverse=True
    )


def test_the_limit_is_respected(db):
    for index in range(8):
        _candidat(f"Profil {index}", experiences=[("Dev", "A", "paiement")])
    assert len(textsearch.search("paiement", limit=3).hits) == 3


def test_the_same_query_gives_the_same_list(db):
    for index in range(4):
        _candidat(f"Profil {index}", experiences=[("Dev", "A", f"paiement {index}")])

    premier = [hit.candidate.pk for hit in textsearch.search("paiement").hits]
    second = [hit.candidate.pk for hit in textsearch.search("paiement").hits]
    assert premier == second


def test_without_embeddings_the_hybrid_falls_back_and_says_so(db):
    _candidat("Cible", experiences=[("Dev", "A", "paiement")])
    resultat = textsearch.search("paiement", hybrid=True)

    assert resultat.semantic_used is False
    assert resultat.hits, "l'hybride degrade doit rester une recherche utile"


# --- Fusion par rang ---------------------------------------------------------
def test_rank_fusion_favours_what_both_lists_agree_on(db):
    commun = _candidat("Commun")
    lexical_seul = _candidat("Lexical")
    vectoriel_seul = _candidat("Vectoriel")

    lexicaux = [
        textsearch.Hit(candidate=lexical_seul, score=9.0, rank=1, method="bm25"),
        textsearch.Hit(candidate=commun, score=5.0, rank=2, method="bm25"),
    ]
    vectoriels = [
        textsearch.Hit(candidate=vectoriel_seul, score=0.9, rank=1, method="vectoriel"),
        textsearch.Hit(candidate=commun, score=0.8, rank=2, method="vectoriel"),
    ]

    fusionnes = textsearch._fusion([lexicaux, vectoriels], limit=3)

    assert fusionnes[0].candidate.pk == commun.pk
    assert fusionnes[0].method == "bm25+vectoriel"


def test_rank_fusion_ignores_score_scales(db):
    """Un score BM25 n'est pas borne, un cosinus vit dans [0, 1] : seuls les
    rangs se comparent."""
    premier = _candidat("Premier")
    second = _candidat("Second")

    enorme = [textsearch.Hit(candidate=second, score=9999.0, rank=2, method="bm25")]
    minuscule = [textsearch.Hit(candidate=premier, score=0.01, rank=1, method="bm25")]

    fusionnes = textsearch._fusion([enorme, minuscule], limit=2)
    assert fusionnes[0].candidate.pk == premier.pk


# --- Harnais -----------------------------------------------------------------
def test_the_harness_meets_its_thresholds(db):
    rapport = search_eval.run()

    assert rapport.failures() == {}, rapport.aggregate
    assert rapport.aggregate["mrr"] > 0


def test_the_harness_leaves_no_trace(db):
    avant = Candidate.objects.count()
    search_eval.run()
    assert Candidate.objects.count() == avant


def test_the_harness_publishes_the_reachable_ceiling(db):
    """Sept profils pertinents pour cinq places : 0.71 est un sans-faute."""
    rapport = search_eval.run()
    python = next(item for item in rapport.queries if item.id == "terme_frequent")

    assert python.recall_at_5_ceiling < 1.0
    assert python.at_ceiling, "la requete atteint son plafond, ce n'est pas un manque"
    assert rapport.aggregate["recall_at_5"] <= rapport.aggregate["recall_at_5_ceiling"]


def test_the_harness_checks_the_empty_query(db):
    rapport = search_eval.run()
    vide = next(item for item in rapport.queries if item.expects_nothing)

    assert vide.answered_nothing
    assert rapport.aggregate["empty_queries_handled"] == 1.0


# --- Interface et API --------------------------------------------------------
def test_the_candidate_list_searches(client, db, recruteur):
    cible = _candidat(
        "Paiement", experiences=[("Ingenieur", "Fintech", "Integration SEPA")]
    )
    _candidat("Autre", experiences=[("Dev", "X", "APIs internes")])

    client.force_login(recruteur)
    reponse = client.get(reverse("candidates:list"), {"q": "SEPA"})

    assert [c.pk for c in reponse.context["candidates"]] == [cible.pk]
    assert reponse.context["search"].corpus_size == 2


def test_the_candidate_list_without_a_query_lists_everyone(client, db, recruteur):
    _candidat("Alice")
    _candidat("Bob")

    client.force_login(recruteur)
    reponse = client.get(reverse("candidates:list"))

    assert len(reponse.context["candidates"]) == 2
    assert reponse.context["search"] is None


def test_the_api_searches(client, db, recruteur):
    _candidat("Paiement", experiences=[("Ingenieur", "Fintech", "Integration SEPA")])

    client.force_login(recruteur)
    donnees = client.get(reverse("api:candidate-search"), {"q": "SEPA"}).json()

    assert donnees["count"] == 1
    assert donnees["results"][0]["rank"] == 1
    assert donnees["results"][0]["matched_terms"] == ["sepa"]
    assert donnees["results"][0]["candidate"]["full_name"] == "Paiement"


def test_the_api_requires_a_query(client, db, recruteur):
    client.force_login(recruteur)
    assert client.get(reverse("api:candidate-search")).status_code == 400


def test_the_api_search_masks_identities_when_blind(client, db, recruteur):
    _candidat("Sara Idrissi", experiences=[("Ingenieur", "Fintech", "SEPA")])
    recruteur.blind_screening = True
    recruteur.save()

    client.force_login(recruteur)
    donnees = client.get(reverse("api:candidate-search"), {"q": "SEPA"}).json()

    assert "Sara" not in donnees["results"][0]["candidate"]["full_name"]
