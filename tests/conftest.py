import pytest
from django.contrib.auth import get_user_model

from apps.jobs.models import JobOffer, JobSkill


@pytest.fixture(autouse=True)
def cache_vide():
    """Vide le cache avant et apres chaque test.

    Le cache memoire de Django vit dans le processus, pas dans la base : la
    transaction annulee a la fin d'un test ne le touche pas. Un rapport de
    biais, un seuil calibre ou un audit mis en cache par un test restaient donc
    visibles du suivant.

    Cela ne s'est vu qu'en ordre aleatoire — huit echecs qui disparaissaient
    des qu'on rejouait la suite dans l'ordre du fichier. Une suite dont le
    resultat depend de l'ordre ne prouve rien, et c'est exactement le genre de
    defaut qu'on met sur le compte du hasard avant de le comprendre.
    """
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def recruiter(db):
    return get_user_model().objects.create_user(
        username="recruteur", password="motdepasse-de-test-123", role="recruiter"
    )


@pytest.fixture
def offer(db, recruiter):
    offer = JobOffer.objects.create(
        title="Ingenieur Backend Python",
        description="Conception d'APIs Django et integration de modeles de langage.",
        department="Technique",
        location="Casablanca",
        experience_min_years=2,
        status=JobOffer.Status.OPEN,
        created_by=recruiter,
    )
    JobSkill.objects.create(offer=offer, name="Python", weight=2.0)
    JobSkill.objects.create(offer=offer, name="Django", weight=1.5)
    JobSkill.objects.create(
        offer=offer, name="Kubernetes", requirement=JobSkill.Requirement.PREFERRED
    )
    return offer
