"""Ce que l'agent fait, etape par etape.

L'agent n'apporte aucune capacite nouvelle : lire un CV, scorer, generer des
questions, rediger une analyse existaient deja et sont testes separement. Il
apporte l'**enchainement** — faire ces choses dans l'ordre, sans qu'un humain
clique sur chaque bouton, en reprenant la ou il s'etait arrete.

Trois proprietes tiennent le module :

**Reprise.** Chaque etape declare ce qui la rend inutile. Un dossier deja score
n'est pas rescore ; un dossier dont les questions existent n'en regenere pas.
Le serveur d'inference de ce projet tombe regulierement : un agent qui
reprendrait tout depuis le debut a chaque relance couterait plusieurs fois le
meme travail.

**Arret net.** Budget epuise ou interrupteur coupe : l'agent s'arrete et le
declare. Il ne degrade pas et ne continue pas « juste ce dossier-la ».

**Il propose, il n'engage rien.** La derniere etape ecrit une recommandation
en attente. Elle ne fait avancer aucune candidature. Le compte de l'agent est
d'ailleurs hors de `can_decide` : meme un appel maladroit a `decide()` serait
refuse.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.ai.client import InferenceError
from apps.candidates.models import Application
from apps.core.models import AuditLog
from apps.core.services import record_audit

from . import budget as budget_module
from .models import AgentRun, Recommendation

logger = logging.getLogger(__name__)

# Ecart au seuil au-dela duquel l'agent se permet une recommandation. Dans la
# bande intermediaire il n'en fait aucune : sur un dossier limite, une
# proposition ecrite peserait plus que le chiffre ne le justifie, et le
# recruteur suivrait une recommandation que rien ne fonde.
MARGE_RECOMMANDATION = 0.10


def compte_agent():
    """Compte sous lequel l'agent agit. Cree au besoin, sans droit de decider."""
    User = get_user_model()
    nom = getattr(settings, "AGENT_USERNAME", "agent")
    compte, cree = User.objects.get_or_create(
        username=nom,
        defaults={
            "first_name": "Agent",
            "last_name": "d'orchestration",
            "role": User.Role.AGENT,
            "is_active": False,  # aucun acces par formulaire de connexion
        },
    )
    if cree:
        compte.set_unusable_password()
        compte.save(update_fields=["password"])
    return compte


@dataclass
class Etape:
    """Une etape du parcours, et ce qui la rend inutile.

    Les fonctions sont designees par leur **nom** et resolues au moment de
    l'appel, pas capturees a l'import. Un tableau d'etapes fige a l'import tient
    les fonctions d'origine : le remplacer devient impossible, et un test qui
    croirait neutraliser une etape appellerait en fait la vraie — en passant
    pour la bonne raison sans l'etre.
    """

    nom: str
    libelle: str
    # Nom de la fonction disant si le travail est deja fait. Vrai = etape sautee.
    faite_par: str
    executee_par: str
    # Une etape qui appelle le modele consomme du budget et peut echouer sur
    # un serveur injoignable ; les autres non.
    appelle_le_modele: bool = False

    def faite(self, application: Application) -> bool:
        return globals()[self.faite_par](application)

    def executer(self, application: Application, run: object) -> None:
        globals()[self.executee_par](application, run)


@dataclass
class Resultat:
    """Ce qu'une execution a produit."""

    run: AgentRun
    arrete_par_le_budget: bool = False
    desactive: bool = False
    journal: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.arrete_par_le_budget or self.desactive)


# --- Les etapes --------------------------------------------------------------
def _score_fait(application: Application) -> bool:
    from apps.matching.engine import ENGINE_VERSION

    dernier = application.scores.order_by("-created_at").first()
    # Un score calcule par une version anterieure du moteur ne compte pas :
    # le classement melangerait alors des chiffres non comparables.
    return dernier is not None and dernier.engine_version == ENGINE_VERSION


def _scorer(application: Application, run: AgentRun) -> None:
    from apps.matching.services import score_application

    score_application(application, with_explanation=False, actor=compte_agent())


def _analyse_faite(application: Application) -> bool:
    dernier = application.scores.order_by("-created_at").first()
    return dernier is not None and bool(dernier.explanation)


def _rediger(application: Application, run: AgentRun) -> None:
    from apps.matching import explain
    from apps.matching.engine import score as calculer

    dernier = application.scores.order_by("-created_at").first()
    if dernier is None:
        return
    resultat = calculer(application.candidate, application.offer)
    analyse = explain.explain(application, resultat)
    if not analyse:
        return
    dernier.explanation = analyse["explanation"]
    dernier.explanation_prompt_id = analyse["prompt_id"]
    dernier.explanation_prompt_version = analyse["prompt_version"]
    dernier.explanation_model = analyse["model"]
    dernier.save(
        update_fields=[
            "explanation", "explanation_prompt_id",
            "explanation_prompt_version", "explanation_model", "updated_at",
        ]
    )


def _questions_faites(application: Application) -> bool:
    """Les questions sont-elles faites — ou inutiles ?

    Inutiles quand l'agent vient de proposer d'ecarter le dossier : preparer un
    entretien pour quelqu'un qu'on propose de ne pas recevoir depense des
    tokens pour un entretien qui n'aura pas lieu. Mesure sur le jeu de
    demonstration : six questions par candidat, soit la moitie du cout d'un
    dossier.

    Si un recruteur passe outre et fait avancer le dossier, l'agent generera
    les questions au passage suivant — le dossier ne sera plus propose au
    rejet.
    """
    if application.interview_questions.exists():
        return True
    return application.recommendations.filter(
        status=Recommendation.Status.PENDING,
        proposed_stage__in=[
            Application.Stage.REJECTED,
            Application.Stage.WITHDRAWN,
        ],
    ).exists()


def _questionner(application: Application, run: AgentRun) -> None:
    from apps.matching import interview
    from apps.matching.engine import score as calculer

    resultat = calculer(application.candidate, application.offer)
    interview.generate(application, resultat, actor=compte_agent())


def _recommandation_faite(application: Application) -> bool:
    return application.recommendations.filter(
        status=Recommendation.Status.PENDING
    ).exists()


def _recommander(application: Application, run: AgentRun) -> None:
    from apps.evaluation import threshold as calibration

    dernier = application.scores.order_by("-created_at").first()
    if dernier is None:
        return

    seuil = calibration.recommended_threshold()
    score = dernier.effective_score
    ecart = score - seuil

    # Bande intermediaire : aucune recommandation. Sur un dossier limite, une
    # proposition ecrite pese plus que le chiffre ne le justifie.
    if abs(ecart) < MARGE_RECOMMANDATION:
        return

    if ecart >= 0:
        etape = Application.Stage.SCREENING
        motif = (
            f"Score de {dernier.percentage} %, soit {ecart * 100:.0f} points "
            f"au-dessus du seuil de {seuil * 100:.0f} %. Toutes les competences "
            f"obligatoires sont couvertes."
            if not dernier.gaps
            else (
                f"Score de {dernier.percentage} %, au-dessus du seuil de "
                f"{seuil * 100:.0f} %, malgre des ecarts sur "
                f"{', '.join(gap['skill'] for gap in dernier.gaps)}."
            )
        )
    else:
        etape = Application.Stage.REJECTED
        ecarts = ", ".join(gap["skill"] for gap in dernier.gaps)
        motif = (
            f"Score de {dernier.percentage} %, soit {abs(ecart) * 100:.0f} points "
            f"sous le seuil de {seuil * 100:.0f} %."
            + (f" Competences obligatoires non couvertes : {ecarts}." if ecarts else "")
            + " Proposition a verifier : le score porte sur le CV, pas sur la personne."
        )

    Recommendation.objects.filter(
        application=application, status=Recommendation.Status.PENDING
    ).update(status=Recommendation.Status.STALE)

    recommandation = Recommendation.objects.create(
        application=application,
        run=run,
        proposed_stage=etape,
        rationale=motif,
        score_at_time=score,
        threshold_at_time=seuil,
        engine_version=dernier.engine_version,
    )
    run.recommendations_made += 1

    record_audit(
        AuditLog.Action.AGENT_RECOMMENDED,
        actor=compte_agent(),
        obj=application,
        summary=f"Recommandation : {etape} ({dernier.percentage} %)",
        agent=True,
        stage=etape,
        score=score,
        threshold=seuil,
        recommendation=str(recommandation.pk),
    )


# L'ordre porte une decision de cout : la recommandation passe AVANT les
# questions d'entretien, pour que celles-ci puissent etre sautees sur un
# dossier propose au rejet. Dans l'ordre inverse, l'agent preparait un
# entretien pour chaque candidat qu'il s'appretait a ecarter.
ETAPES: list[Etape] = [
    Etape("score", "Calcul du score", "_score_fait", "_scorer"),
    Etape(
        "analyse", "Analyse redigee", "_analyse_faite", "_rediger",
        appelle_le_modele=True,
    ),
    Etape("recommandation", "Recommandation", "_recommandation_faite", "_recommander"),
    Etape(
        "questions", "Questions d'entretien", "_questions_faites", "_questionner",
        appelle_le_modele=True,
    ),
]


# --- Execution ---------------------------------------------------------------
def a_traiter():
    """Candidatures que l'agent peut preparer.

    Un dossier deja tranche est laisse tranquille : preparer un entretien pour
    quelqu'un qu'on a ecarte n'a pas de sens, et regenerer des questions sur un
    dossier clos couterait des tokens pour rien.
    """
    return (
        Application.objects.filter(stage=Application.Stage.RECEIVED)
        .select_related("candidate", "offer")
        .prefetch_related("scores", "interview_questions", "recommendations")
        .order_by("applied_at")
    )


@dataclass(frozen=True)
class Dossier:
    """Un dossier vu par la reprise : ce qui est fait, ce qui manque."""

    application: Application
    faites: list[str]
    manquantes: list[str]

    @property
    def jamais_touche(self) -> bool:
        """Simplement en attente, pas en echec."""
        return not self.faites

    @property
    def bloque_sur_le_modele(self) -> bool:
        """Ne manquent que des etapes qui appellent le modele.

        C'est la distinction qui rend la liste exploitable. Un dossier auquel
        il manque le score revele un defaut : le calcul est deterministe et
        local, il n'avait aucune raison d'echouer. Un dossier auquel il ne
        manque que l'analyse redigee revele un serveur d'inference injoignable
        — rien a corriger, la reprise suffira.
        """
        if not self.manquantes:
            return False
        modele = {etape.nom for etape in ETAPES if etape.appelle_le_modele}
        return set(self.manquantes) <= modele


def incomplets(limit: int | None = None) -> list[Dossier]:
    """Dossiers dont il reste au moins une etape a faire.

    Les executions comptent les etapes en echec, ce qui repond a « combien ».
    Un exploitant a besoin de « lesquels » : un compteur a 3 sans moyen de
    savoir quels dossiers sont concernes ne se traite pas.

    Les dossiers deja entames passent devant. Un dossier a moitie prepare est
    ce qui trompe : il a un score, il s'affiche comme les autres, et il lui
    manque l'analyse sur laquelle un recruteur croit s'appuyer.
    """
    dossiers: list[Dossier] = []
    for application in a_traiter():
        faites, manquantes = [], []
        for etape in ETAPES:
            (faites if etape.faite(application) else manquantes).append(etape.nom)
        if manquantes:
            dossiers.append(
                Dossier(application=application, faites=faites, manquantes=manquantes)
            )

    dossiers.sort(key=lambda item: (item.jamais_touche, item.application.applied_at))
    return dossiers[:limit] if limit else dossiers


def run(
    *,
    applications=None,
    trigger: str = AgentRun.Trigger.MANUAL,
    started_by=None,
    limit: int | None = None,
) -> Resultat:
    """Fait avancer les dossiers en attente, sans en trancher aucun."""
    depart = time.perf_counter()
    execution = AgentRun.objects.create(trigger=trigger, started_by=started_by)
    resultat = Resultat(run=execution)

    if not budget_module.agent_actif():
        execution.status = AgentRun.Status.DISABLED
        execution.save(update_fields=["status", "updated_at"])
        resultat.desactive = True
        resultat.journal.append("Agent desactive (AGENT_ENABLED).")
        return resultat

    lot = list(applications if applications is not None else a_traiter())
    if limit:
        lot = lot[:limit]
    execution.applications_seen = len(lot)

    for application in lot:
        # Le budget est relu avant chaque dossier : une execution longue peut
        # le franchir en cours de route, et s'en apercevoir a la fin serait
        # trop tard.
        if budget_module.actuel().epuise:
            execution.status = AgentRun.Status.BUDGET
            resultat.arrete_par_le_budget = True
            resultat.journal.append(
                f"Budget epuise apres {execution.applications_processed} dossier(s)."
            )
            break

        touche = False
        for etape in ETAPES:
            try:
                if etape.faite(application):
                    continue
                etape.executer(application, execution)
                execution.steps_done += 1
                touche = True
            except InferenceError as exc:
                # Le serveur d'inference tombe regulierement. On note et on
                # continue : le score, lui, n'en depend pas, et la reprise
                # rattrapera l'etape manquante au prochain passage.
                execution.steps_failed += 1
                resultat.journal.append(
                    f"{application.pk} · {etape.nom} : serveur indisponible ({exc})"
                )
            except Exception as exc:  # noqa: BLE001
                execution.steps_failed += 1
                resultat.journal.append(f"{application.pk} · {etape.nom} : {exc}")
                logger.exception("Agent, etape %s", etape.nom)

        if touche:
            execution.applications_processed += 1

    if execution.status == AgentRun.Status.RUNNING:
        execution.status = AgentRun.Status.DONE

    execution.duration_ms = int((time.perf_counter() - depart) * 1000)
    execution.tokens_used = budget_module.consommation(
        depuis=execution.created_at
    )
    execution.detail = {"journal": resultat.journal[:50]}
    execution.save()

    record_audit(
        AuditLog.Action.AGENT_RAN,
        actor=started_by or compte_agent(),
        summary=(
            f"Agent : {execution.applications_processed}/{execution.applications_seen} "
            f"dossier(s), {execution.recommendations_made} recommandation(s)"
        ),
        agent=True,
        run=str(execution.pk),
        status=execution.status,
        trigger=trigger,
        steps_done=execution.steps_done,
        steps_failed=execution.steps_failed,
        tokens=execution.tokens_used,
        duration_ms=execution.duration_ms,
    )
    return resultat


def perimer_les_recommandations(application: Application) -> int:
    """Perime les propositions faites sur un score qui n'est plus le dernier.

    Presenter a un recruteur une recommandation calculee sur des chiffres
    perimes serait le pousser a decider sur autre chose que ce qu'il voit.
    """
    dernier = application.scores.order_by("-created_at").first()
    if dernier is None:
        return 0
    return Recommendation.objects.filter(
        application=application, status=Recommendation.Status.PENDING
    ).exclude(score_at_time=dernier.effective_score).update(
        status=Recommendation.Status.STALE
    )


def resoudre(
    recommendation: Recommendation, *, accepter: bool, actor, note: str = "", request=None
) -> Recommendation:
    """Un recruteur tranche une proposition.

    **C'est ici que la decision devient reelle**, et elle est imputee a
    l'humain — pas a l'agent. La proposition n'a servi qu'a preparer le
    terrain ; celui qui repond de la decision devant le candidat est celui qui
    a cliqué.
    """
    from apps.matching.services import DecisionRefused, decide

    if not recommendation.pending:
        raise DecisionRefused("Cette recommandation a deja ete tranchee.")
    if actor is None or not getattr(actor, "can_decide", False):
        raise DecisionRefused("Ce compte n'est pas habilite a trancher.")

    if accepter:
        motif = (note or "").strip() or recommendation.rationale
        decide(
            recommendation.application,
            stage=recommendation.proposed_stage,
            note=motif,
            actor=actor,
            request=request,
        )
        recommendation.status = Recommendation.Status.ACCEPTED
    else:
        recommendation.status = Recommendation.Status.REJECTED

    recommendation.resolved_by = actor
    recommendation.resolved_at = timezone.now()
    recommendation.resolution_note = (note or "").strip()
    recommendation.save(
        update_fields=[
            "status", "resolved_by", "resolved_at", "resolution_note", "updated_at",
        ]
    )
    return recommendation
