"""Interface des echanges : rediger, relire, envoyer, consigner.

Une contrainte structure toutes ces vues : **rediger et envoyer sont deux
actions separees**, avec une page entre les deux. Un bouton « envoyer le
message suggere » aurait fait partir des courriers rediges par un modele sans
qu'un humain les lise — exactement ce que le reste du projet refuse pour les
decisions.
"""

from __future__ import annotations

from django.contrib import messages as flash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView, TemplateView

from apps.candidates.models import Application
from apps.core.permissions import ActionPermissionMixin

from . import backends, registry, services, silence
from .exceptions import EnvoiRefuse
from .models import Channel, Consent, Message


class SilenceView(LoginRequiredMixin, TemplateView):
    """Ce que le processus n'a pas dit aux candidats."""

    template_name = "outreach/silence.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        mesure = silence.mesurer()
        context["mesure"] = mesure
        context["oublis"] = mesure.oublis[:25]
        context["jours_avant_silence"] = silence.JOURS_AVANT_SILENCE
        context["recents"] = (
            Message.objects.select_related("application__candidate", "sent_by")
            .exclude(status=Message.Status.DRAFT)
            .order_by("-created_at")[:15]
        )
        context["canaux"] = [
            {
                "channel": channel,
                "libelle": libelle,
                "connecte": backends.canal_connecte(channel),
                "etat": backends.etat_du_canal(channel),
                "sur_accord": channel in services.CANAUX_SUR_ACCORD,
            }
            for channel, libelle in Channel.choices
            if channel != Channel.OTHER
        ]
        return context


class ThreadView(LoginRequiredMixin, DetailView):
    """Fil complet d'une candidature, et etat des canaux."""

    model = Application
    template_name = "outreach/thread.html"
    context_object_name = "application"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["messages_"] = services.echanges(self.object)
        context["canaux"] = services.canaux_ouverts(self.object.candidate)
        context["modeles"] = registry.disponibles()
        context["consent_sources"] = Consent.Source.choices
        return context


class DraftView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Prepare un brouillon. N'envoie rien."""

    def post(self, request, pk):
        application = get_object_or_404(
            Application.objects.select_related("candidate", "offer"), pk=pk
        )
        modele_id = request.POST.get("modele", "")
        channel = request.POST.get("channel", Channel.EMAIL)

        try:
            message = services.rediger(
                application,
                modele_id=modele_id,
                channel=channel,
                actor=request.user,
                avec_modele=request.POST.get("sans_modele") != "1",
                question=request.POST.get("question", ""),
                motif=request.POST.get("motif", ""),
            )
        except (KeyError, ValueError) as exc:
            flash.error(request, str(exc))
            return redirect("outreach:thread", pk=pk)

        if message.redige_par_un_modele:
            flash.success(
                request,
                "Brouillon personnalise par le modele a partir du gabarit. "
                "Relisez-le : rien ne part tant que vous ne l'envoyez pas.",
            )
        else:
            flash.info(
                request,
                "Brouillon issu du gabarit versionne. Le modele n'a pas ete "
                "sollicite ou n'a pas repondu — le texte reste utilisable tel "
                "quel.",
            )
        return redirect("outreach:thread", pk=pk)


class SendView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Envoie un brouillon, apres l'avoir eventuellement corrige."""

    def post(self, request, pk):
        message = get_object_or_404(
            Message.objects.select_related("application__candidate"), pk=pk
        )
        retour = redirect("outreach:thread", pk=message.application_id)

        corps = request.POST.get("body", "").strip()
        if corps and corps != message.body and message.modifiable:
            message.body = corps
            message.subject = request.POST.get("subject", message.subject)
            # Un texte repris a la main n'est plus la sortie du modele : le
            # journal ne doit pas continuer de l'attribuer au prompt.
            message.prompt_id = ""
            message.prompt_version = ""
            message.model_name = ""
            message.save(
                update_fields=[
                    "body", "subject", "prompt_id", "prompt_version",
                    "model_name", "updated_at",
                ]
            )

        try:
            services.envoyer(message, actor=request.user, request=request)
        except EnvoiRefuse as exc:
            flash.error(request, str(exc))
        except Exception as exc:  # noqa: BLE001
            flash.error(request, f"Envoi impossible : {exc}")
        else:
            flash.success(
                request,
                f"Message envoye a {message.application.candidate.full_name}.",
            )
        return retour


class LogView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Consigne un appel ou un message recu hors du systeme."""

    def post(self, request, pk):
        application = get_object_or_404(Application, pk=pk)
        corps = request.POST.get("body", "").strip()
        if not corps:
            flash.error(request, "Un echange consigne sans contenu n'apprend rien.")
            return redirect("outreach:thread", pk=pk)

        services.consigner(
            application,
            channel=request.POST.get("channel", Channel.CALL),
            body=corps,
            direction=request.POST.get("direction", Message.Direction.INBOUND),
            actor=request.user,
            request=request,
            duree_min=request.POST.get("duree", ""),
        )
        flash.success(
            request,
            "Echange consigne. Il compte comme une reponse au candidat au meme "
            "titre qu'un message envoye.",
        )
        return redirect("outreach:thread", pk=pk)


class ConsentView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Enregistre un accord ou un retrait de contact."""

    def post(self, request, pk):
        application = get_object_or_404(
            Application.objects.select_related("candidate"), pk=pk
        )
        channel = request.POST.get("channel", "")
        accorde = request.POST.get("granted") == "1"
        if channel not in dict(Channel.choices):
            flash.error(request, "Canal inconnu.")
            return redirect("outreach:thread", pk=pk)

        services.enregistrer_consentement(
            application.candidate,
            channel=channel,
            granted=accorde,
            actor=request.user,
            source=(
                Consent.Source.VERBAL if accorde else Consent.Source.WITHDRAWN
            ),
            note=request.POST.get("note", ""),
            request=request,
        )
        flash.success(
            request,
            f"{'Accord' if accorde else 'Retrait'} enregistre pour "
            f"{Channel(channel).label}. L'enregistrement precedent est conserve.",
        )
        return redirect("outreach:thread", pk=pk)
