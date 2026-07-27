"""Metriques de qualite du classement.

Implementees a la main, sans scipy : ce sont quelques lignes, elles sont
testees, et cela evite une dependance de 40 Mo pour trois formules.

Convention de pertinence, attribuee a la main dans les jeux d'evaluation :

    3  excellent  — a recevoir en entretien sans hesitation
    2  bon        — merite un entretien
    1  faible     — profil eloigne mais pas absurde
    0  hors sujet
"""

from __future__ import annotations

import math
from collections.abc import Sequence

RELEVANT_FROM = 2


def dcg(gains: Sequence[float]) -> float:
    """Gain cumule actualise. Un bon candidat place en 1re position vaut plus
    que le meme candidat place en 5e."""
    return sum(gain / math.log2(rank + 2) for rank, gain in enumerate(gains))


def ndcg_at_k(ranked_relevances: Sequence[int], k: int = 5) -> float:
    """DCG normalise : 1.0 si le classement produit est le classement ideal.

    `ranked_relevances` est la suite des pertinences dans l'ordre **predit**
    par le moteur.
    """
    if not ranked_relevances:
        return 0.0
    actual = dcg(list(ranked_relevances)[:k])
    ideal = dcg(sorted(ranked_relevances, reverse=True)[:k])
    return actual / ideal if ideal else 0.0


def precision_at_k(ranked_relevances: Sequence[int], k: int = 3) -> float:
    """Part de candidats reellement pertinents dans les k premiers.

    Le denominateur est borne par le nombre de pertinents disponibles : sur un
    cas ne comptant que deux bons profils, un P@3 parfait doit valoir 1.0.
    """
    if not ranked_relevances:
        return 0.0
    top = list(ranked_relevances)[:k]
    available = sum(1 for value in ranked_relevances if value >= RELEVANT_FROM)
    if available == 0:
        return 1.0 if not any(value >= RELEVANT_FROM for value in top) else 0.0
    found = sum(1 for value in top if value >= RELEVANT_FROM)
    return found / min(k, available)


def pair_accuracy(ranked_relevances: Sequence[int]) -> float:
    """Part de paires correctement ordonnees.

    La metrique la plus directement interpretable : « sur toutes les paires de
    candidats de pertinence differente, quelle proportion le moteur a-t-il
    classee dans le bon sens ? » 0.5 = hasard, 1.0 = parfait.
    """
    values = list(ranked_relevances)
    concordant = comparable = 0
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            if values[i] == values[j]:
                continue
            comparable += 1
            # i precede j dans le classement predit : correct si i est meilleur.
            concordant += int(values[i] > values[j])
    return concordant / comparable if comparable else 1.0


def spearman(predicted: Sequence[float], expected: Sequence[float]) -> float:
    """Correlation de rang de Spearman, dans [-1, 1]."""
    if len(predicted) != len(expected):
        raise ValueError("Les deux suites doivent avoir la meme longueur.")
    if len(predicted) < 2:
        return 1.0

    left, right = _ranks(predicted), _ranks(expected)
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)

    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True)
    )
    variance_left = sum((a - mean_left) ** 2 for a in left)
    variance_right = sum((b - mean_right) ** 2 for b in right)

    denominator = math.sqrt(variance_left * variance_right)
    return covariance / denominator if denominator else 0.0


def _ranks(values: Sequence[float]) -> list[float]:
    """Rangs moyens, ex aequo geres (indispensable : les pertinences le sont)."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2 + 1
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


# --- Extraction -------------------------------------------------------------
def set_prf(predicted: set[str], expected: set[str]) -> dict[str, float]:
    """Precision, rappel et F1 sur des ensembles (competences, langues...)."""
    if not predicted and not expected:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    true_positives = len(predicted & expected)
    precision = true_positives / len(predicted) if predicted else 0.0
    recall = true_positives / len(expected) if expected else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return {"precision": precision, "recall": recall, "f1": f1}
