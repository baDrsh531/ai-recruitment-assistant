"""Jeu de donnees de demonstration.

    python manage.py seed_demo

Cree un compte recruteur, deux offres et quelques candidats afin d'avoir une
interface non vide des le premier lancement.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.candidates.models import (
    Application,
    Candidate,
    CandidateLanguage,
    CandidateSkill,
    Experience,
)
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

        # Une repostulation : meme personne, six mois plus tard, CV remanie et
        # nom saisi dans l'autre sens. C'est le cas que la page « Doublons »
        # existe pour attraper, et il n'a rien d'exotique — un candidat qui
        # repostule ne se souvient pas de l'adresse qu'il avait utilisee.
        repostulation, creee = Candidate.objects.get_or_create(
            email="s.elamrani@example.com",
            defaults={
                "full_name": "EL AMRANI Sara",
                "headline": "Ingenieure data",
                "total_experience_years": 5.0,
                "highest_education": EducationLevel.MASTER,
                "location": "Maroc",
                "phone": "+212 661 22 33 44",
            },
        )
        if creee:
            for name, years in [("Python", 5.0), ("SQL", 6.0), ("dbt", 1.5)]:
                CandidateSkill.objects.create(
                    candidate=repostulation, name=name, years=years, last_used_year=2026
                )
            CandidateLanguage.objects.create(
                candidate=repostulation, language="Anglais", level=LanguageLevel.C1
            )
            Experience.objects.create(
                candidate=repostulation, title="Data engineer", company="OCP Group"
            )
            Application.objects.get_or_create(candidate=repostulation, offer=offers[1])

        # Le dossier d'origine recoit le meme employeur : sans signal partage
        # au-dela du nom, les deux resteraient — a juste titre — separes.
        originale = Candidate.objects.filter(email="sara.elamrani@example.com").first()
        if originale is not None:
            originale.phone = originale.phone or "0661223344"
            originale.save(update_fields=["phone"])
            Experience.objects.get_or_create(
                candidate=originale, title="Data engineer", company="OCP Group"
            )

        self._decisions(recruiter)
        self._recommandations(recruiter)
        self._echanges(recruiter)
        self._historique_moteur()

        self.stdout.write(
            self.style.SUCCESS(
                f"{JobOffer.objects.count()} offres, {Candidate.objects.count()} candidats, "
                f"{Application.objects.count()} candidatures."
            )
        )

    def _decisions(self, premier) -> None:
        """Deux recruteurs decidant sur les memes dossiers.

        Sans un second evaluateur, la page d'accord ne peut rien mesurer et
        affiche « non mesurable » — ce qui est correct mais ne montre pas ce
        qu'elle sait faire. Les desaccords sont deliberes : deux recruteurs qui
        seraient toujours d'accord rendraient la mesure inutile, et ce n'est pas
        ce qu'on observe en pratique.
        """
        from apps.core.models import AuditLog
        from apps.matching.services import DecisionRefused, decide

        if AuditLog.objects.filter(action=AuditLog.Action.STAGE_CHANGED).exists():
            self.stdout.write("Historique de decisions deja present.")
            return

        User = get_user_model()
        second, cree = User.objects.get_or_create(
            username="manager",
            defaults={
                "email": "manager@example.com",
                "first_name": "Youssef",
                "last_name": "Berrada",
                "role": User.Role.HIRING_MANAGER,
                "department": "Technique",
            },
        )
        if cree:
            second.set_password(DEMO_PASSWORD)
            second.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Compte cree : manager / {DEMO_PASSWORD} (manager operationnel)"
                )
            )

        MOTIF = "Profil trop eloigne du poste, verifie sur le CV complet."
        # (recruteur, etape) par candidature. Les deux derniers dossiers
        # opposent les deux evaluateurs : c'est ce que le kappa mesure.
        PARCOURS = [
            [(premier, "screening"), (second, "screening")],
            [(premier, "phone"), (second, "screening")],
            [(premier, "technical"), (second, "technical")],
            [(premier, "rejected"), (second, "rejected")],
            [(premier, "rejected"), (second, "screening")],
            [(premier, "screening"), (second, "rejected")],
        ]

        candidatures = list(
            Application.objects.select_related("candidate").order_by("applied_at")
        )
        for candidature, etapes in zip(candidatures, PARCOURS, strict=False):
            for acteur, etape in etapes:
                try:
                    decide(candidature, stage=etape, note=MOTIF, actor=acteur)
                except DecisionRefused as exc:
                    self.stdout.write(self.style.WARNING(f"  decision refusee : {exc}"))

        self.stdout.write(
            f"{AuditLog.objects.filter(action='stage_changed').count()} decisions "
            "journalisees, par deux evaluateurs."
        )

    def _recommandations(self, premier) -> None:
        """Historique de propositions deja tranchees par des humains.

        Meme raison que pour les decisions ci-dessus : sans historique, la page
        de l'agent affiche « rien ne permet de dire si la supervision est
        effective », ce qui est exact et ne montre rien. Il faut au moins vingt
        propositions tranchees pour que le taux devienne lisible.

        La repartition est deliberee, et c'est elle qui fait la demonstration :
        les recruteurs contredisent souvent les propositions de rejet et
        valident presque toujours les mises en entretien. C'est exactement le
        genre d'ecart que le taux global masque et que la ventilation par type
        est faite pour montrer — une supervision qui se relache la ou elle
        engage le moins.
        """
        import datetime as dt
        import itertools

        from django.utils import timezone

        from apps.agent.models import Recommendation

        if Recommendation.objects.exclude(
            status=Recommendation.Status.PENDING
        ).exists():
            self.stdout.write("Historique de recommandations deja present.")
            return

        User = get_user_model()
        second = User.objects.filter(username="manager").first() or premier

        candidatures = list(Application.objects.select_related("candidate"))
        if not candidatures:
            return

        # (etape proposee, suivie ?) — 26 propositions, assez pour que le taux
        # sorte de la zone ou l'intervalle de confiance interdit de conclure.
        PLAN = (
            [("rejected", False)] * 5
            + [("rejected", True)] * 8
            + [("screening", True)] * 11
            + [("screening", False)] * 2
        )

        maintenant = timezone.now()
        roue = itertools.cycle(candidatures)
        for index, (etape, suivie) in enumerate(PLAN):
            candidature = next(roue)
            propose = maintenant - dt.timedelta(days=30 - index, minutes=index * 7)
            recommandation = Recommendation.objects.create(
                application=candidature,
                proposed_stage=etape,
                rationale=(
                    "Score sous le seuil, competences obligatoires incompletes."
                    if etape == "rejected"
                    else "Score au-dessus du seuil, competences obligatoires couvertes."
                ),
                score_at_time=0.42 if etape == "rejected" else 0.88,
                threshold_at_time=0.85,
                status=(
                    Recommendation.Status.ACCEPTED
                    if suivie
                    else Recommendation.Status.REJECTED
                ),
                resolved_by=premier if index % 2 else second,
                resolution_note=(
                    "" if suivie else "Relu le CV complet : la proposition rate le contexte."
                ),
            )
            # `created_at` est pose automatiquement ; il faut le reecrire pour
            # que le delai median de decision ne soit pas nul partout.
            Recommendation.objects.filter(pk=recommandation.pk).update(
                created_at=propose,
                resolved_at=propose + dt.timedelta(minutes=35 + (index % 7) * 25),
            )

        self.stdout.write(
            f"{len(PLAN)} recommandations tranchees par un humain, dont "
            f"{sum(1 for _, suivie in PLAN if not suivie)} contredites."
        )

    def _historique_moteur(self) -> None:
        """Une decision prise sous une version anterieure du moteur.

        Sans elle, la page de rejeu n'affiche qu'un seul de ses deux resultats :
        « aucun ecart ». C'est le bon resultat, mais il ne montre pas ce que la
        page sait diagnostiquer — d'ou vient un ecart quand il y en a un.

        Le score enregistre est donc recule de quelques points et estampille
        1.1.0. Cela represente ce qui arrive vraiment : une application qui a
        traverse un changement de moteur, et des dossiers tranches avant. Le
        chiffre est fabrique, comme le reste du jeu de demonstration, et le
        README le dit.
        """
        from apps.matching.models import MatchScore

        ancien = (
            MatchScore.objects.filter(engine_version="1.1.0").exists()
        )
        if ancien:
            self.stdout.write("Historique de moteur deja present.")
            return

        candidature = (
            Application.objects.filter(
                stage__in=("rejected", "withdrawn", "hired"),
                decided_at__isnull=False,
            )
            .order_by("decided_at")
            .first()
        )
        if candidature is None:
            return

        # Le dernier score calcule AVANT la decision : c'est celui-la que le
        # rejeu retient, pas le plus ancien ni le plus recent. Viser un autre
        # laisserait le semis sans effet visible.
        score = (
            candidature.scores.filter(created_at__lte=candidature.decided_at)
            .order_by("-created_at")
            .first()
        )
        if score is None:
            return

        MatchScore.objects.filter(pk=score.pk).update(
            engine_version="1.1.0",
            # Assez pour se voir, trop peu pour franchir le seuil : la page
            # distingue un ecart d'une bascule, et les deux meritent d'etre
            # illustres separement.
            overall=max(0.0, score.overall - 0.04),
        )
        self.stdout.write(
            "1 decision estampillee moteur 1.1.0, pour que le rejeu ait une "
            "transition de version a diagnostiquer."
        )

    def _echanges(self, recruteur) -> None:
        """Quelques echanges, et surtout quelques silences.

        La page des echanges se lit mal a vide : un taux de silence de 0 % sur
        zero dossier ressemble a un taux exemplaire. On seme donc les deux
        etats — un candidat ecarte puis prevenu, un autre ecarte et laisse sans
        reponse — pour que la mesure ait quelque chose a montrer et que le
        contraste soit visible.

        Les messages sont ecrits directement en base plutot qu'expedies : un
        peuplement de demonstration n'a pas a passer par la couche courriel.
        """
        import datetime as dt

        from django.utils import timezone

        from apps.outreach import registry, services
        from apps.outreach.models import Channel, Consent, Message

        if Message.objects.exists():
            self.stdout.write("Echanges deja presents.")
            return

        dossiers = list(
            Application.objects.select_related("candidate", "offer").order_by(
                "applied_at"
            )
        )
        if not dossiers:
            return

        maintenant = timezone.now()

        # Un accord explicite sur un canal ferme par defaut, et un retrait sur
        # un canal presume ouvert : les deux sens de la regle sont visibles.
        services.enregistrer_consentement(
            dossiers[0].candidate, channel=Channel.WHATSAPP, granted=True,
            actor=recruteur, source=Consent.Source.FORM,
            note="Case cochee sur le formulaire de candidature.",
        )
        if len(dossiers) > 1:
            services.enregistrer_consentement(
                dossiers[1].candidate, channel=Channel.CALL, granted=False,
                actor=recruteur, source=Consent.Source.WITHDRAWN,
                note="Demande a ne plus etre appele pendant ses heures de travail.",
            )

        modele = registry.get("accuse_reception")
        for candidature in dossiers[:3]:
            rendu = modele.rendre(
                **services.valeurs_par_defaut(candidature, actor=recruteur)
            )
            quand = maintenant - dt.timedelta(days=20, hours=3)
            message = Message.objects.create(
                application=candidature,
                channel=Channel.EMAIL,
                direction=Message.Direction.OUTBOUND,
                status=Message.Status.SENT,
                subject=rendu["subject"],
                body=rendu["body"],
                template_id=rendu["template_id"],
                template_version=rendu["template_version"],
                drafted_by=recruteur,
                sent_by=recruteur,
                sent_at=quand,
            )
            Message.objects.filter(pk=message.pk).update(created_at=quand)

        # Un appel consigne : il compte comme une reponse au meme titre qu'un
        # e-mail expedie, et c'est ce que la mesure du silence doit montrer.
        ecartes = [
            candidature for candidature in dossiers
            if candidature.stage == Application.Stage.REJECTED
            and candidature.decided_at
        ]
        if ecartes:
            services.consigner(
                ecartes[0],
                channel=Channel.CALL,
                body=(
                    "Refus annonce par telephone. Le candidat a demande le "
                    "detail des criteres ; explication envoyee dans la foulee."
                ),
                actor=recruteur,
                direction=Message.Direction.OUTBOUND,
            )

        self.stdout.write(
            f"{Message.objects.count()} echanges semes, "
            f"{Consent.objects.count()} consentements. "
            f"{len(ecartes[1:])} dossier(s) ecarte(s) volontairement laisse(s) "
            f"sans reponse, pour que la mesure du silence montre les deux etats."
        )
