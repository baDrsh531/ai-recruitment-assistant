"""Candidats, CV et donnees extraites.

Principe structurant : **rien n'est extrait sans preuve**. Chaque competence,
experience ou diplome pointe vers un `EvidenceSpan` — un extrait verbatim du
document, avec sa page et ses coordonnees. Une affirmation sans preuve est
rejetee a l'ecriture. C'est ce qui rend l'analyse verifiable en un clic et ce
qui empeche une hallucination d'atterrir dans un dossier de candidature.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

from apps.core.models import BaseModel
from apps.jobs.models import EducationLevel, JobOffer, LanguageLevel


def cv_upload_path(instance: CVDocument, filename: str) -> str:
    return f"cv/{instance.content_hash[:2]}/{instance.content_hash}/{filename}"


class Candidate(BaseModel):
    """Personne physique. Soumis au RGPD : purge automatique via `retention_until`."""

    full_name = models.CharField("nom complet", max_length=200)
    email = models.EmailField(blank=True, db_index=True)
    phone = models.CharField("telephone", max_length=40, blank=True)
    linkedin_url = models.URLField("LinkedIn", blank=True)
    github_url = models.URLField("GitHub", blank=True)
    portfolio_url = models.URLField("Portfolio", blank=True)
    location = models.CharField("localisation", max_length=160, blank=True)

    headline = models.CharField("titre professionnel", max_length=200, blank=True)
    total_experience_years = models.FloatField("experience totale (annees)", default=0.0)
    highest_education = models.IntegerField(
        "plus haut diplome", choices=EducationLevel.choices, default=EducationLevel.NONE
    )

    consent_given_at = models.DateTimeField("consentement recueilli le", null=True, blank=True)
    retention_until = models.DateField("conservation jusqu'au", null=True, blank=True)

    class Meta:
        verbose_name = "candidat"
        verbose_name_plural = "candidats"
        ordering = ("full_name",)
        indexes = [models.Index(fields=["email"]), models.Index(fields=["retention_until"])]

    def __str__(self) -> str:
        return self.full_name

    def get_absolute_url(self) -> str:
        return reverse("candidates:detail", kwargs={"pk": self.pk})

    def save(self, *args, **kwargs):
        """Fixe la date de fin de conservation a la creation.

        Le champ existait, indexe, avec un reglage `DATA_RETENTION_DAYS` en
        face — mais rien ne les reliait : aucune date n'etait ecrite, aucune
        purge ne tournait. Un dossier de candidature ne se conserve pas
        indefiniment, et une echeance qui n'est jamais posee ne se respecte pas.
        """
        if self.retention_until is None:
            from django.conf import settings

            self.retention_until = dt.date.today() + dt.timedelta(
                days=settings.DATA_RETENTION_DAYS
            )
        return super().save(*args, **kwargs)

    @property
    def days_until_purge(self) -> int | None:
        if self.retention_until is None:
            return None
        return (self.retention_until - dt.date.today()).days

    @property
    def retention_expired(self) -> bool:
        return self.days_until_purge is not None and self.days_until_purge < 0

    def display_name(self, *, blind: bool = False) -> str:
        """Nom masque en mode screening a l'aveugle."""
        if not blind:
            return self.full_name
        return f"Candidat {str(self.pk)[:8].upper()}"


class CVDocument(BaseModel):
    """Fichier de CV et resultat de son extraction.

    `content_hash` est unique : redeposer le meme fichier ne relance aucun
    traitement, on reutilise l'extraction existante.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "En attente"
        EXTRACTING = "extracting", "Extraction en cours"
        PARSING = "parsing", "Structuration en cours"
        DONE = "done", "Termine"
        FAILED = "failed", "Echec"

    class Method(models.TextChoices):
        TEXT = "text", "Texte natif (PyMuPDF)"
        DOCX = "docx", "DOCX (python-docx)"
        VISION = "vision", "Vision (Qwen3-VL)"
        HYBRID = "hybrid", "Texte + vision"

    candidate = models.ForeignKey(
        Candidate, null=True, blank=True, on_delete=models.CASCADE, related_name="documents"
    )
    file = models.FileField("fichier", upload_to=cv_upload_path)
    original_filename = models.CharField(max_length=255)
    content_hash = models.CharField(max_length=64, unique=True, db_index=True)
    size_bytes = models.PositiveIntegerField(default=0)
    page_count = models.PositiveSmallIntegerField(default=0)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    method = models.CharField(max_length=8, choices=Method.choices, blank=True)
    error = models.TextField(blank=True)

    raw_text = models.TextField(blank=True)
    # Indices de qualite de l'extraction : proportion de caracteres exploitables,
    # detection d'une mise en page multi-colonnes, besoin d'OCR.
    quality = models.JSONField(default=dict, blank=True)

    extraction_started_at = models.DateTimeField(null=True, blank=True)
    extraction_finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "CV"
        verbose_name_plural = "CV"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.original_filename

    @property
    def extraction_seconds(self) -> float | None:
        if not (self.extraction_started_at and self.extraction_finished_at):
            return None
        return (self.extraction_finished_at - self.extraction_started_at).total_seconds()

    @property
    def evidence_verifiable(self) -> bool:
        """Les citations peuvent-elles etre confrontees au document ?

        Sur un CV scanne, il n'y a aucune couche texte : le modele vision lit
        une image, et rien ne permet de retrouver ses citations dans le
        document. Presenter alors les donnees comme « non etayees » serait
        trompeur — elles ne sont pas contredites, elles sont invérifiables.
        La nuance doit apparaitre a l'ecran comme dans les mesures.
        """
        return bool(self.quality.get("has_text_layer", True))


class EvidenceSpan(BaseModel):
    """Extrait verbatim du CV justifiant une donnee extraite.

    `bbox` contient les coordonnees PyMuPDF [x0, y0, x1, y1] en points, ce qui
    permet de surligner le passage exact dans la visionneuse PDF.
    """

    document = models.ForeignKey(CVDocument, on_delete=models.CASCADE, related_name="spans")
    page = models.PositiveSmallIntegerField(default=1)
    text = models.TextField()
    bbox = models.JSONField(null=True, blank=True)
    char_start = models.PositiveIntegerField(null=True, blank=True)
    char_end = models.PositiveIntegerField(null=True, blank=True)
    # Faux si la citation du modele n'a pas pu etre retrouvee dans le document.
    verified = models.BooleanField(default=False)
    # Qualite de la correspondance : 1.0 pour un extrait retrouve mot pour mot,
    # 0.75 pour une citation breve mais exacte, en dessous pour une
    # correspondance partielle. Ce qui distingue une citation peu bavarde d'une
    # citation inventee.
    match_ratio = models.FloatField("qualite de la correspondance", default=0.0)

    class Meta:
        verbose_name = "preuve"
        verbose_name_plural = "preuves"
        ordering = ("page", "char_start")

    def __str__(self) -> str:
        return f"p.{self.page} · {self.text[:60]}"


class EvidencedModel(BaseModel):
    """Base des donnees extraites : toute donnee porte sa preuve."""

    evidence = models.ForeignKey(
        EvidenceSpan, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    confidence = models.FloatField(
        default=1.0, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)]
    )

    class Meta:
        abstract = True


class CandidateSkill(EvidencedModel):
    class Source(models.TextChoices):
        DECLARED = "declared", "Declaree (section competences)"
        INFERRED = "inferred", "Deduite d'une experience"
        ONTOLOGY = "ontology", "Deduite par l'ontologie"

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="skills")
    name = models.CharField("competence", max_length=120)
    normalized_name = models.CharField(max_length=120, db_index=True)
    esco_uri = models.URLField(blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.DECLARED)
    years = models.FloatField("annees de pratique", default=0.0)
    last_used_year = models.PositiveSmallIntegerField(null=True, blank=True)
    embedding = models.BinaryField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "competence du candidat"
        verbose_name_plural = "competences du candidat"
        ordering = ("-years", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "normalized_name"], name="unique_skill_per_candidate"
            )
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.normalized_name:
            self.normalized_name = self.name.strip().lower()
        return super().save(*args, **kwargs)

    @property
    def recency_factor(self) -> float:
        """Une competence non pratiquee depuis 6 ans compte moins qu'une competence actuelle."""
        if not self.last_used_year:
            return 0.85
        gap = dt.date.today().year - self.last_used_year
        if gap <= 1:
            return 1.0
        if gap <= 3:
            return 0.9
        if gap <= 6:
            return 0.75
        return 0.55


class Experience(EvidencedModel):
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="experiences")
    title = models.CharField("poste", max_length=200)
    company = models.CharField("entreprise", max_length=200, blank=True)
    location = models.CharField(max_length=160, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField("fin (vide si en cours)", null=True, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = "experience"
        verbose_name_plural = "experiences"
        ordering = ("-start_date",)

    def __str__(self) -> str:
        return f"{self.title} — {self.company}"

    @property
    def duration_years(self) -> float:
        if not self.start_date:
            return 0.0
        end = self.end_date or dt.date.today()
        return max(0.0, (end - self.start_date).days / 365.25)

    @property
    def is_current(self) -> bool:
        return self.start_date is not None and self.end_date is None


class Education(EvidencedModel):
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="education")
    degree = models.CharField("diplome", max_length=200)
    field_of_study = models.CharField("specialite", max_length=200, blank=True)
    institution = models.CharField("etablissement", max_length=200, blank=True)
    level = models.IntegerField(choices=EducationLevel.choices, default=EducationLevel.NONE)
    graduation_year = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = "formation"
        verbose_name_plural = "formations"
        ordering = ("-graduation_year",)

    def __str__(self) -> str:
        return self.degree


class CandidateLanguage(EvidencedModel):
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="languages")
    language = models.CharField("langue", max_length=60)
    level = models.CharField(max_length=3, choices=LanguageLevel.choices, default=LanguageLevel.B1)

    class Meta:
        verbose_name = "langue"
        verbose_name_plural = "langues"
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "language"], name="unique_language_per_candidate"
            )
        ]

    def __str__(self) -> str:
        return f"{self.language} ({self.level})"


class Certification(EvidencedModel):
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="certifications"
    )
    name = models.CharField("certification", max_length=200)
    issuer = models.CharField("organisme", max_length=200, blank=True)
    obtained_year = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = "certification"
        verbose_name_plural = "certifications"
        ordering = ("-obtained_year",)

    def __str__(self) -> str:
        return self.name


class Application(BaseModel):
    """Candidature : le lien entre un candidat, une offre et un CV."""

    class Stage(models.TextChoices):
        RECEIVED = "received", "Recue"
        SCREENING = "screening", "Pre-selection"
        PHONE = "phone", "Entretien telephonique"
        TECHNICAL = "technical", "Entretien technique"
        FINAL = "final", "Entretien final"
        OFFER = "offer", "Proposition"
        HIRED = "hired", "Recrute"
        REJECTED = "rejected", "Ecarte"
        WITHDRAWN = "withdrawn", "Desistement"

    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="applications"
    )
    offer = models.ForeignKey(JobOffer, on_delete=models.CASCADE, related_name="applications")
    document = models.ForeignKey(
        CVDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name="applications"
    )

    stage = models.CharField(max_length=12, choices=Stage.choices, default=Stage.RECEIVED)
    applied_at = models.DateTimeField(auto_now_add=True)

    # Toute sortie du processus est imputee a un humain : l'IA classe, elle ne
    # rejette jamais seule (exigence de supervision humaine, AI Act art. 14).
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL, related_name="decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField("motif de la decision", blank=True)

    class Meta:
        verbose_name = "candidature"
        verbose_name_plural = "candidatures"
        ordering = ("-applied_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "offer"], name="unique_application_per_offer"
            )
        ]
        indexes = [models.Index(fields=["offer", "stage"])]

    def __str__(self) -> str:
        return f"{self.candidate} → {self.offer}"

    def get_absolute_url(self) -> str:
        return reverse("candidates:application_detail", kwargs={"pk": self.pk})

    @property
    def is_closed(self) -> bool:
        return self.stage in {self.Stage.HIRED, self.Stage.REJECTED, self.Stage.WITHDRAWN}
