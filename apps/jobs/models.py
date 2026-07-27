from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from apps.core.models import BaseModel

# Ponderation par defaut du score de compatibilite.
# Elle est deterministe et modifiable offre par offre : c'est le recruteur qui
# decide de ce qui compte, pas le modele.
DEFAULT_WEIGHTS = {
    "skills": 0.45,
    "experience": 0.20,
    "education": 0.10,
    "languages": 0.10,
    "certifications": 0.05,
    "location": 0.10,
}


class EducationLevel(models.IntegerChoices):
    NONE = 0, "Sans exigence"
    HIGH_SCHOOL = 1, "Baccalaureat"
    BACHELOR = 3, "Bac+3 / Licence"
    MASTER = 5, "Bac+5 / Master"
    PHD = 8, "Doctorat"


class LanguageLevel(models.TextChoices):
    A1 = "A1", "A1 — Debutant"
    A2 = "A2", "A2 — Elementaire"
    B1 = "B1", "B1 — Intermediaire"
    B2 = "B2", "B2 — Intermediaire superieur"
    C1 = "C1", "C1 — Avance"
    C2 = "C2", "C2 — Maitrise"
    NATIVE = "NAT", "Langue maternelle"


LANGUAGE_LEVEL_ORDER = {
    LanguageLevel.A1: 1, LanguageLevel.A2: 2, LanguageLevel.B1: 3,
    LanguageLevel.B2: 4, LanguageLevel.C1: 5, LanguageLevel.C2: 6,
    LanguageLevel.NATIVE: 7,
}


class JobOffer(BaseModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Brouillon"
        OPEN = "open", "Ouverte"
        PAUSED = "paused", "Suspendue"
        CLOSED = "closed", "Cloturee"

    class RemotePolicy(models.TextChoices):
        ONSITE = "onsite", "Sur site"
        HYBRID = "hybrid", "Hybride"
        REMOTE = "remote", "Teletravail total"

    class ContractType(models.TextChoices):
        CDI = "cdi", "CDI"
        CDD = "cdd", "CDD"
        INTERNSHIP = "internship", "Stage"
        APPRENTICESHIP = "apprenticeship", "Alternance"
        FREELANCE = "freelance", "Freelance"

    title = models.CharField("intitule", max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    description = models.TextField()
    department = models.CharField("departement", max_length=120, blank=True)
    location = models.CharField("localisation", max_length=160, blank=True)
    remote_policy = models.CharField(
        "teletravail", max_length=10, choices=RemotePolicy.choices, default=RemotePolicy.HYBRID
    )
    contract_type = models.CharField(
        "contrat", max_length=16, choices=ContractType.choices, default=ContractType.CDI
    )

    salary_min = models.PositiveIntegerField("salaire min", null=True, blank=True)
    salary_max = models.PositiveIntegerField("salaire max", null=True, blank=True)
    currency = models.CharField(max_length=3, default="EUR")

    experience_min_years = models.PositiveSmallIntegerField("experience min (annees)", default=0)
    experience_max_years = models.PositiveSmallIntegerField(
        "experience max (annees)", null=True, blank=True
    )
    education_level = models.IntegerField(
        "niveau d'etudes", choices=EducationLevel.choices, default=EducationLevel.NONE
    )

    required_certifications = models.JSONField(
        "certifications exigees",
        default=list,
        blank=True,
        help_text="Liste de noms, ex. [\"AWS Solutions Architect\"]. Vide = critere non applicable.",
    )

    deadline = models.DateField("date limite", null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)

    scoring_weights = models.JSONField(
        "ponderation du score",
        default=dict,
        blank=True,
        help_text="Surcharge la ponderation par defaut. Laisser vide pour l'heriter.",
    )
    blind_screening = models.BooleanField(
        "screening a l'aveugle",
        default=False,
        help_text=(
            "Exclut la localisation du calcul du score et masque les employeurs "
            "dans l'analyse redigee. La politique appartient a l'offre, non au "
            "recruteur : le score doit etre identique pour tous ceux qui la consultent."
        ),
    )
    embedding = models.BinaryField(null=True, blank=True, editable=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="job_offers"
    )

    class Meta:
        verbose_name = "offre d'emploi"
        verbose_name_plural = "offres d'emploi"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)[:200] or "offre"
            slug, suffix = base, 1
            while JobOffer.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix += 1
                slug = f"{base}-{suffix}"
            self.slug = slug
        return super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("jobs:detail", kwargs={"slug": self.slug})

    @property
    def weights(self) -> dict[str, float]:
        """Ponderation effective, normalisee pour sommer a 1."""
        merged = {**DEFAULT_WEIGHTS, **(self.scoring_weights or {})}
        total = sum(merged.values()) or 1.0
        return {key: value / total for key, value in merged.items()}

    @property
    def required_skills(self):
        return self.skills.filter(requirement=JobSkill.Requirement.REQUIRED)

    @property
    def preferred_skills(self):
        return self.skills.filter(requirement=JobSkill.Requirement.PREFERRED)

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.OPEN


class JobSkill(BaseModel):
    class Requirement(models.TextChoices):
        REQUIRED = "required", "Obligatoire"
        PREFERRED = "preferred", "Souhaitee"

    offer = models.ForeignKey(JobOffer, on_delete=models.CASCADE, related_name="skills")
    name = models.CharField("competence", max_length=120)
    # Forme canonique issue de la taxonomie ESCO ; permet de rapprocher
    # "DRF", "Django REST Framework" et "django-rest-framework".
    normalized_name = models.CharField(max_length=120, blank=True, db_index=True)
    esco_uri = models.URLField(blank=True)
    requirement = models.CharField(
        max_length=10, choices=Requirement.choices, default=Requirement.REQUIRED
    )
    weight = models.FloatField(
        "poids relatif",
        default=1.0,
        validators=[MinValueValidator(0.1), MaxValueValidator(5.0)],
    )
    min_years = models.PositiveSmallIntegerField("anciennete min", default=0)
    embedding = models.BinaryField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "competence attendue"
        verbose_name_plural = "competences attendues"
        ordering = ("requirement", "-weight", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["offer", "normalized_name"], name="unique_skill_per_offer"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_requirement_display()})"

    def save(self, *args, **kwargs):
        if not self.normalized_name:
            self.normalized_name = self.name.strip().lower()
        return super().save(*args, **kwargs)


class JobLanguage(BaseModel):
    offer = models.ForeignKey(JobOffer, on_delete=models.CASCADE, related_name="languages")
    language = models.CharField("langue", max_length=60)
    min_level = models.CharField(
        "niveau minimum", max_length=3, choices=LanguageLevel.choices, default=LanguageLevel.B2
    )
    is_required = models.BooleanField("obligatoire", default=True)

    class Meta:
        verbose_name = "langue attendue"
        verbose_name_plural = "langues attendues"
        constraints = [
            models.UniqueConstraint(
                fields=["offer", "language"], name="unique_language_per_offer"
            )
        ]

    def __str__(self) -> str:
        return f"{self.language} {self.min_level}"
