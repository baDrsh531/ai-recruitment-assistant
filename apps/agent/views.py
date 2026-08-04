"""Interface de l'agent : ce qu'il a fait, ce qu'il propose, et son frein."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from apps.core.permissions import ActionPermissionMixin
from apps.matching.services import DecisionRefused

from . import budget, pipeline
from .models import AgentRun, Recommendation


class AgentDashboardView(LoginRequiredMixin, TemplateView):
    """Qu'a fait la machine, et combien ca a coute.

    C'est la premiere question d'un responsable devant un systeme qui appelle
    un modele tout seul. Un agent qui tourne sans ce releve est ingerable.
    """

    template_name = "agent/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        executions = AgentRun.objects.all()[:20]

        context["runs"] = executions
        context["budget"] = budget.actuel()
        context["actif"] = budget.agent_actif()
        context["pending"] = (
            Recommendation.objects.filter(status=Recommendation.Status.PENDING)
            .select_related("application__candidate", "application__offer")
            .order_by("-created_at")[:15]
        )
        context["waiting"] = pipeline.a_traiter().count()

        totaux = AgentRun.objects.aggregate(
            tokens=Sum("tokens_used"),
            traites=Sum("applications_processed"),
            recommandations=Sum("recommendations_made"),
            echecs=Sum("steps_failed"),
        )
        context["totaux"] = {cle: valeur or 0 for cle, valeur in totaux.items()}
        return context


class ResolveRecommendationView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Un recruteur tranche une proposition de l'agent.

    C'est ici que la decision devient reelle, et elle est imputee a l'humain.
    La proposition n'a servi qu'a preparer le terrain.
    """

    def post(self, request, pk):
        recommandation = get_object_or_404(
            Recommendation.objects.select_related("application__candidate"), pk=pk
        )
        accepter = request.POST.get("action") == "accepter"

        try:
            pipeline.resoudre(
                recommandation,
                accepter=accepter,
                actor=request.user,
                note=request.POST.get("note", ""),
                request=request,
            )
        except DecisionRefused as exc:
            messages.error(request, str(exc))
        else:
            nom = recommandation.application.candidate.full_name
            messages.success(
                request,
                f"{nom} : recommandation {'suivie' if accepter else 'ecartee'}. "
                "La decision est enregistree a votre nom.",
            )

        retour = request.POST.get("retour")
        if retour:
            return redirect(retour)
        return redirect(
            "candidates:application_detail", pk=recommandation.application_id
        )


class RunAgentView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Lance l'agent a la main depuis l'interface."""

    def post(self, request):
        if not budget.agent_actif():
            messages.warning(
                request,
                "L'agent est desactive. Le reglage AGENT_ENABLED le remet en "
                "marche, sans redeploiement.",
            )
            return redirect("agent:dashboard")

        resultat = pipeline.run(
            trigger=AgentRun.Trigger.MANUAL, started_by=request.user, limit=25
        )
        execution = resultat.run

        if resultat.arrete_par_le_budget:
            messages.error(
                request,
                "Budget epuise : l'agent s'est arrete. Les dossiers restants "
                "seront repris a la prochaine execution, sans refaire ce qui "
                "est deja fait.",
            )
        else:
            messages.success(
                request,
                f"{execution.applications_processed} dossier(s) prepares, "
                f"{execution.recommendations_made} recommandation(s) en attente. "
                "Aucune candidature n'a avance.",
            )
        return redirect("agent:dashboard")
