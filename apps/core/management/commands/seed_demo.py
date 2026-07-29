"""Jeu de donnees de demonstration.

    python manage.py seed_demo

Cree un compte recruteur, deux offres et quelques candidats afin d'avoir une
interface non vide des le premier lancement.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.candidates.models import Application, Candidate, CandidateLanguage, CandidateSkill
from apps.jobs.models import EducationLevel, JobLanguage, JobOffer, JobSkill, LanguageLevel

DEMO_PASSWORD = "demo-recrutement-2026"

OFFERS = [
    {
        "title": "Ingenieur Backend Python / IA",
        "description": (
            "Vous concevez et exploitez les APIs Django de la plateforme et integrez "
            "des modeles de langage dans les parcours produit. Vous travaillez avec "
            "l'equipe data sur la recherche semantique et l'evaluation des modeles."
        ),
        "department": "Technique",
        "location": "Casablanca",
        "experience_min_years": 2,
        "education_level": EducationLevel.BACHELOR,
        "required": [("Python", 2.0, 2), ("Django", 1.5, 2), ("PostgreSQL", 1.0, 1)],
        "preferred": [("Docker", 1.0), ("LLM", 1.5), ("Kubernetes", 0.8)],
        "languages": [("Francais", LanguageLevel.C1), ("Anglais", LanguageLevel.B2)],
    },
    {
        "title": "Data Engineer",
        "description": (
            "Construction et maintenance des pipelines de donnees, modelisation "
            "analytique et fiabilisation de la qualite des donnees."
        ),
        "department": "Data",
        "location": "Rabat",
        "experience_min_years": 3,
        "education_level": EducationLevel.MASTER,
        "required": [("Python", 1.5, 3), ("SQL", 2.0, 3), ("Airflow", 1.0, 1)],
        "preferred": [("Spark", 1.0), ("dbt", 0.8)],
        "languages": [("Anglais", LanguageLevel.B2)],
    },
]

CANDIDATES = [
    {
        "full_name": "Ahmed Benali",
        "email": "ahmed.benali@example.com",
        "headline": "Ingenieur logiciel backend",
        "years": 5.0,
        "education": EducationLevel.MASTER,
        "skills": [("Python", 5.0), ("Django", 4.0), ("PostgreSQL", 4.0), ("Docker", 3.0)],
        "languages": [("Francais", LanguageLevel.NATIVE), ("Anglais", LanguageLevel.C1)],
    },
    {
        "full_name": "Sara El Amrani",
        "email": "sara.elamrani@example.com",
        "headline": "Data engineer",
        "years": 4.0,
        "education": EducationLevel.MASTER,
        "skills": [("Python", 4.0), ("SQL", 5.0), ("Airflow", 2.0), ("Spark", 2.0)],
        "languages": [("Francais", LanguageLevel.C2), ("Anglais", LanguageLevel.C1)],
    },
    {
        "full_name": "Badr Sahraoui",
        "email": "badr.sahraoui@example.com",
        "headline": "Developpeur backend et IA appliquee",
        "years": 2.0,
        "education": EducationLevel.BACHELOR,
        "skills": [("Python", 2.0), ("Django", 2.0), ("SQL", 1.5), ("LLM", 1.0)],
        "languages": [("Francais", LanguageLevel.NATIVE), ("Anglais", LanguageLevel.B2)],
    },
]


class Command(BaseCommand):
    help = "Cree un jeu de donnees de demonstration."

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        recruiter, created = User.objects.get_or_create(
            username="recruteur",
            defaults={
                "email": "recruteur@example.com",
                "first_name": "Nadia",
                "last_name": "Cherkaoui",
                "role": User.Role.RECRUITER,
                "department": "Ressources humaines",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            recruiter.set_password(DEMO_PASSWORD)
            recruiter.save()
            self.stdout.write(
                self.style.SUCCESS(f"Compte cree : recruteur / {DEMO_PASSWORD}")
            )
        else:
            self.stdout.write("Compte 'recruteur' deja present.")

        # Un compte en lecture seule, pour verifier que le controle d'acces
        # fait bien quelque chose : consulter oui, agir non.
        observateur, cree = User.objects.get_or_create(
            username="observateur",
            defaults={
                "email": "observateur@example.com",
                "first_name": "Yassine",
                "last_name": "Bennani",
                "role": User.Role.VIEWER,
                "department": "Direction",
            },
        )
        if cree:
            observateur.set_password(DEMO_PASSWORD)
            observateur.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Compte cree : observateur / {DEMO_PASSWORD} (lecture seule)"
                )
            )

        offers = []
        for spec in OFFERS:
            offer, made = JobOffer.objects.get_or_create(
                title=spec["title"],
                defaults={
                    "description": spec["description"],
                    "department": spec["department"],
                    "location": spec["location"],
                    "experience_min_years": spec["experience_min_years"],
                    "education_level": spec["education_level"],
                    "status": JobOffer.Status.OPEN,
                    "created_by": recruiter,
                },
            )
            offers.append(offer)
            if not made:
                continue
            for name, weight, min_years in spec["required"]:
                JobSkill.objects.create(
                    offer=offer, name=name, weight=weight, min_years=min_years,
                    requirement=JobSkill.Requirement.REQUIRED,
                )
            for name, weight in spec["preferred"]:
                JobSkill.objects.create(
                    offer=offer, name=name, weight=weight,
                    requirement=JobSkill.Requirement.PREFERRED,
                )
            for language, level in spec["languages"]:
                JobLanguage.objects.create(offer=offer, language=language, min_level=level)

        for spec in CANDIDATES:
            candidate, made = Candidate.objects.get_or_create(
                email=spec["email"],
                defaults={
                    "full_name": spec["full_name"],
                    "headline": spec["headline"],
                    "total_experience_years": spec["years"],
                    "highest_education": spec["education"],
                    "location": "Maroc",
                },
            )
            if made:
                for name, years in spec["skills"]:
                    CandidateSkill.objects.create(
                        candidate=candidate, name=name, years=years, last_used_year=2026
                    )
                for language, level in spec["languages"]:
                    CandidateLanguage.objects.create(
                        candidate=candidate, language=language, level=level
                    )
            Application.objects.get_or_create(candidate=candidate, offer=offers[0])

        self.stdout.write(
            self.style.SUCCESS(
                f"{JobOffer.objects.count()} offres, {Candidate.objects.count()} candidats, "
                f"{Application.objects.count()} candidatures."
            )
        )
