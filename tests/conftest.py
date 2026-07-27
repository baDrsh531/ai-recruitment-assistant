import pytest
from django.contrib.auth import get_user_model

from apps.jobs.models import JobOffer, JobSkill


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
