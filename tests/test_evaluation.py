"""Tests du harnais d'evaluation.

Deux niveaux : les metriques (valeurs connues, calculees a la main) et le
harnais lui-meme, qui sert de test de non-regression du moteur. Si une
modification du scoring degrade le classement, c'est ici que cela se voit.
"""

from __future__ import annotations

import json

import pytest

from apps.candidates.models import Candidate
from apps.evaluation import harness, metrics
from apps.jobs.models import JobOffer
from apps.matching import engine


# --- Metriques --------------------------------------------------------------
def test_ndcg_is_one_for_perfect_ranking():
    assert metrics.ndcg_at_k([3, 2, 1, 0], 5) == pytest.approx(1.0)


def test_ndcg_is_lower_for_inverted_ranking():
    perfect = metrics.ndcg_at_k([3, 2, 1, 0], 5)
    inverted = metrics.ndcg_at_k([0, 1, 2, 3], 5)
    assert inverted < perfect
    assert 0 < inverted < 1


def test_ndcg_penalises_late_placement_of_best_candidate():
    """Le meilleur candidat en 3e position coute plus qu'en 2e."""
    second = metrics.ndcg_at_k([2, 3, 1], 5)
    third = metrics.ndcg_at_k([2, 1, 3], 5)
    assert third < second < 1.0


def test_ndcg_handles_empty_and_all_zero():
    assert metrics.ndcg_at_k([], 5) == 0.0
    assert metrics.ndcg_at_k([0, 0, 0], 5) == 0.0


def test_precision_at_k_counts_relevant_in_top():
    assert metrics.precision_at_k([3, 2, 0, 0], 3) == pytest.approx(1.0)
    assert metrics.precision_at_k([3, 0, 0, 2], 3) == pytest.approx(0.5)


def test_precision_at_k_bounded_by_available_relevant():
    """Un seul bon profil existe et il est premier : la precision vaut 1."""
    assert metrics.precision_at_k([3, 1, 0, 0], 3) == pytest.approx(1.0)


def test_pair_accuracy_extremes():
    assert metrics.pair_accuracy([3, 2, 1, 0]) == pytest.approx(1.0)
    assert metrics.pair_accuracy([0, 1, 2, 3]) == pytest.approx(0.0)
    # Une seule paire inversee sur six.
    assert metrics.pair_accuracy([3, 1, 2, 0]) == pytest.approx(5 / 6)


def test_pair_accuracy_ignores_ties():
    assert metrics.pair_accuracy([2, 2, 2]) == pytest.approx(1.0)


def test_spearman_extremes():
    assert metrics.spearman([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert metrics.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_handles_ties():
    """Les pertinences comportent des ex aequo : les rangs moyens sont requis."""
    assert metrics.spearman([0.9, 0.8, 0.4], [3, 3, 1]) == pytest.approx(0.866, abs=1e-3)


def test_spearman_rejects_length_mismatch():
    with pytest.raises(ValueError):
        metrics.spearman([1, 2], [1, 2, 3])


def test_set_prf():
    result = metrics.set_prf({"python", "django", "go"}, {"python", "django", "sql"})
    assert result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == pytest.approx(2 / 3)
    assert result["f1"] == pytest.approx(2 / 3)
    assert metrics.set_prf(set(), set())["f1"] == 1.0
    assert metrics.set_prf({"a"}, set())["f1"] == 0.0


# --- Jeu de donnees ---------------------------------------------------------
def test_dataset_is_available():
    assert "ranking_v1" in harness.available_datasets()


def test_dataset_is_well_formed():
    dataset = harness.load_dataset("ranking_v1")
    assert dataset["cases"]

    identifiers = [case["id"] for case in dataset["cases"]]
    assert len(identifiers) == len(set(identifiers)), "identifiants de cas dupliques"

    for case in dataset["cases"]:
        candidates = case["candidates"]
        assert len(candidates) >= 3, f"{case['id']} : trop peu de candidats"
        assert all(0 <= item["relevance"] <= 3 for item in candidates)
        # Un cas sans variete de pertinence ne mesure rien.
        assert len({item["relevance"] for item in candidates}) >= 2, case["id"]
        assert case["offer"]["required_skills"], case["id"]


def test_unknown_dataset_raises():
    with pytest.raises(FileNotFoundError):
        harness.load_dataset("inexistant")


def test_only_ranking_datasets_are_listed():
    """Regression : `evaluate` balayait tout le dossier `datasets/`.

    L'ajout d'un jeu d'extraction dans le meme dossier faisait lire ce dernier
    comme un jeu de classement, et la commande echouait sur une cle absente.
    Chaque fichier declare desormais sa nature.
    """
    from apps.evaluation import extraction

    listed = harness.available_datasets()
    assert "ranking_v1" in listed
    assert "extraction_v1" not in listed

    with pytest.raises(ValueError, match="evaluate_extraction"):
        harness.load_dataset("extraction_v1")
    with pytest.raises(ValueError, match="manage.py evaluate"):
        extraction.load_dataset("ranking_v1")


# --- Harnais ----------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture(scope="module")
def report_cache():
    return {}


def test_harness_meets_thresholds(db):
    """Test de non-regression du moteur de classement.

    C'est le garde-fou principal : toute modification du scoring qui degrade
    le classement sous les seuils fait echouer la suite.
    """
    report = harness.run("ranking_v1")
    failures = report.failures()
    assert not failures, "Metriques sous leur seuil : " + ", ".join(
        f"{name} = {value:.3f} < {threshold}"
        for name, (value, threshold) in failures.items()
    )


def test_harness_leaves_no_data_behind(db):
    """Evaluer ne doit rien ecrire dans les donnees de travail."""
    offers_before = JobOffer.objects.count()
    candidates_before = Candidate.objects.count()

    harness.run("ranking_v1")

    assert JobOffer.objects.count() == offers_before
    assert Candidate.objects.count() == candidates_before


def test_harness_reports_engine_version(db):
    report = harness.run("ranking_v1")
    assert report.engine_version == engine.ENGINE_VERSION
    assert report.dataset == "ranking_v1"
    assert len(report.cases) == len(harness.load_dataset("ranking_v1")["cases"])


def test_report_is_json_serialisable(db):
    report = harness.run("ranking_v1")
    restored = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
    assert restored["aggregate"]["ndcg_at_5"] == report.aggregate["ndcg_at_5"]
    assert restored["cases"][0]["predicted_order"]


def test_comparison_detects_regression(db):
    report = harness.run("ranking_v1")
    baseline = report.as_dict()
    baseline["aggregate"] = {
        name: value + 0.10 for name, value in baseline["aggregate"].items()
    }

    deltas = harness.compare(report, baseline)
    assert all(values["delta"] < 0 for values in deltas.values())


def test_comparison_ignores_unknown_metrics(db):
    report = harness.run("ranking_v1")
    assert harness.compare(report, {"aggregate": {"metrique_inconnue": 1.0}}) == {}
