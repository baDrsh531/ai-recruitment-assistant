"""Ou couper le classement.

Le moteur ordonne les candidatures, il ne dit pas laquelle est la derniere a
recevoir. En pratique le recruteur coupe quand meme — au feeling, ou sur un
chiffre rond. Ce module remplace le chiffre rond par une mesure : sur le jeu
annote a la main, on balaie tous les seuils possibles et on regarde, pour
chacun, qui passe et qui est ecarte a tort.

**Le cout de l'erreur est asymetrique**, et c'est tout l'enjeu. Recevoir un
candidat moyen coute une heure d'entretien. Ecarter un bon candidat coute un
recrutement — et le cout est supporte par quelqu'un qui n'en saura jamais rien.
Le seuil recommande maximise donc un F-beta avec beta = 2, qui pese le rappel
quatre fois plus que la precision. Ce choix est un jugement, pas un resultat :
il est expose en constante et le tableau complet reste affiche, pour qu'un
recruteur puisse trancher autrement en connaissance de cause.

Ce que le module ne fait pas : appliquer le seuil. Aucune candidature n'est
ecartee automatiquement, ici moins qu'ailleurs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from apps.matching import engine

from .harness import _temporary_case, load_dataset

# Pertinence a partir de laquelle un candidat aurait du etre recu. Le jeu
# annote note de 0 a 3 ; 2 signifie « merite un entretien ».
SHORTLIST_FROM = 2

# Poids du rappel face a la precision. beta = 2 : manquer un bon profil est
# tenu pour quatre fois plus grave que recevoir un profil moyen.
BETA = 2.0

# Pas du balayage. Plus fin serait une precision illusoire au regard de la
# taille du jeu annote.
STEP = 0.01


@dataclass
class Point:
    """Un seuil candidat et ce qu'il produit sur le jeu annote."""

    threshold: float
    retained: int
    true_positive: int
    false_positive: int
    false_negative: int
    precision: float
    recall: float
    f_beta: float

    @property
    def threshold_percentage(self) -> int:
        return round(self.threshold * 100)

    @property
    def missed(self) -> int:
        """Bons profils ecartes : le chiffre qui doit rester sous les yeux."""
        return self.false_negative


@dataclass
class Calibration:
    dataset: str
    dataset_version: str
    engine_version: str
    beta: float
    shortlist_from: int
    curve: list[Point] = field(default_factory=list)
    recommended: Point | None = None
    total_relevant: int = 0
    total_candidates: int = 0
    # Bornes de l'intervalle sur lequel le F-beta est maximal.
    plateau_low: float = 0.0
    plateau_high: float = 0.0

    @property
    def recommended_percentage(self) -> int:
        return self.recommended.threshold_percentage if self.recommended else 0

    @property
    def plateau_low_percentage(self) -> int:
        return round(self.plateau_low * 100)

    @property
    def plateau_high_percentage(self) -> int:
        return round(self.plateau_high * 100)

    @property
    def plateau_width_points(self) -> int:
        return self.plateau_high_percentage - self.plateau_low_percentage

    @property
    def perfectly_separable(self) -> bool:
        """Aucun bon profil manque et aucun mauvais retenu au seuil recommande.

        C'est un resultat a lire avec mefiance : il dit autant de la facilite du
        jeu annote que de la qualite du moteur.
        """
        return bool(
            self.recommended
            and self.recommended.false_negative == 0
            and self.recommended.false_positive == 0
        )

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "engine_version": self.engine_version,
            "beta": self.beta,
            "shortlist_from": self.shortlist_from,
            "total_candidates": self.total_candidates,
            "total_relevant": self.total_relevant,
            "recommended": asdict(self.recommended) if self.recommended else None,
            "plateau": [self.plateau_low, self.plateau_high],
            "perfectly_separable": self.perfectly_separable,
            "curve": [asdict(point) for point in self.curve],
        }


def _observations(dataset: dict) -> list[tuple[float, bool]]:
    """(score du moteur, ce candidat meritait-il un entretien) pour tout le jeu."""
    couples: list[tuple[float, bool]] = []
    for case in dataset["cases"]:
        with _temporary_case(case) as (offer, pairs):
            for spec, candidate in pairs:
                resultat = engine.score(candidate, offer)
                couples.append(
                    (resultat.overall, spec["relevance"] >= SHORTLIST_FROM)
                )
    return couples


def _point(seuil: float, observations: list[tuple[float, bool]]) -> Point:
    vrais_positifs = sum(1 for score, bon in observations if score >= seuil and bon)
    faux_positifs = sum(1 for score, bon in observations if score >= seuil and not bon)
    faux_negatifs = sum(1 for score, bon in observations if score < seuil and bon)

    retenus = vrais_positifs + faux_positifs
    precision = vrais_positifs / retenus if retenus else 0.0
    rappel = (
        vrais_positifs / (vrais_positifs + faux_negatifs)
        if (vrais_positifs + faux_negatifs)
        else 0.0
    )

    beta_carre = BETA * BETA
    denominateur = beta_carre * precision + rappel
    f_beta = (
        (1 + beta_carre) * precision * rappel / denominateur if denominateur else 0.0
    )

    return Point(
        threshold=round(seuil, 4),
        retained=retenus,
        true_positive=vrais_positifs,
        false_positive=faux_positifs,
        false_negative=faux_negatifs,
        precision=round(precision, 4),
        recall=round(rappel, 4),
        f_beta=round(f_beta, 4),
    )


def calibrate(dataset_name: str = "ranking_v1") -> Calibration:
    """Balaie les seuils et recommande celui qui maximise le F-beta."""
    dataset = load_dataset(dataset_name)
    observations = _observations(dataset)

    calibration = Calibration(
        dataset=dataset["name"],
        dataset_version=dataset.get("version", "0"),
        engine_version=engine.ENGINE_VERSION,
        beta=BETA,
        shortlist_from=SHORTLIST_FROM,
        total_candidates=len(observations),
        total_relevant=sum(1 for _, bon in observations if bon),
    )
    if not observations:
        return calibration

    pas = int(round(1 / STEP))
    calibration.curve = [_point(index / pas, observations) for index in range(pas + 1)]

    # Le F-beta est maximal sur tout un intervalle, pas en un point : entre deux
    # scores consecutifs, rien ne change. Retenir une borne de cet intervalle
    # serait fragile — au bord haut, la moindre baisse de score fait perdre un
    # bon profil. On prend donc le milieu, et on publie les bornes pour que la
    # marge reelle soit visible plutot que devinee.
    meilleur = max(point.f_beta for point in calibration.curve)
    plateau = [point for point in calibration.curve if point.f_beta == meilleur]
    calibration.plateau_low = plateau[0].threshold
    calibration.plateau_high = plateau[-1].threshold

    milieu = (calibration.plateau_low + calibration.plateau_high) / 2
    calibration.recommended = min(
        plateau, key=lambda point: (abs(point.threshold - milieu), point.threshold)
    )
    return calibration


# Le balayage represente un scoring complet du jeu annote : deterministe,
# lent a l'echelle d'une page, et invalide de lui-meme des que le moteur change.
CACHE_KEY = f"evaluation:threshold:{engine.ENGINE_VERSION}"
CACHE_SECONDS = 60 * 60


def cached(dataset_name: str = "ranking_v1") -> Calibration:
    """Calibration en cache, pour un affichage sur une page de travail."""
    from django.core.cache import cache

    cle = f"{CACHE_KEY}:{dataset_name}"
    calibration = cache.get(cle)
    if calibration is None:
        calibration = calibrate(dataset_name)
        cache.set(cle, calibration, CACHE_SECONDS)
    return calibration


def recommended_threshold(dataset_name: str = "ranking_v1") -> float:
    """Seuil de coupe recommande, ou la valeur par defaut si rien n'est mesurable."""
    calibration = cached(dataset_name)
    if calibration.recommended is None:
        return 0.75
    return calibration.recommended.threshold


def sampled_curve(calibration: Calibration, step: float = 0.05) -> list[Point]:
    """Sous-echantillonnage lisible de la courbe, pour l'affichage."""
    facteur = int(round(step / STEP))
    points = calibration.curve[::facteur]
    if calibration.recommended and calibration.recommended not in points:
        points.append(calibration.recommended)
        points.sort(key=lambda point: point.threshold)
    return points
