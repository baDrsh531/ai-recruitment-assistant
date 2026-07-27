from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count
from django.views.generic import DetailView, ListView, TemplateView

from apps.ai.models import AIInvocation
from apps.jobs.models import JobOffer

from .models import Application, Candidate, CVDocument


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stats"] = {
            "candidates": Candidate.objects.count(),
            "open_offers": JobOffer.objects.filter(status=JobOffer.Status.OPEN).count(),
            "applications": Application.objects.count(),
            "documents_pending": CVDocument.objects.exclude(
                status=CVDocument.Status.DONE
            ).count(),
        }
        context["ai"] = AIInvocation.objects.aggregate(
            calls=Count("id"), avg_latency=Avg("latency_ms")
        )
        context["stages"] = (
            Application.objects.values("stage").annotate(total=Count("id")).order_by("-total")
        )
        context["recent_offers"] = JobOffer.objects.order_by("-created_at")[:5]
        return context


class CandidateListView(LoginRequiredMixin, ListView):
    model = Candidate
    template_name = "candidates/candidate_list.html"
    context_object_name = "candidates"
    paginate_by = 25

    def get_queryset(self):
        return Candidate.objects.prefetch_related("skills").order_by("full_name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["blind"] = self.request.user.blind_screening
        return context


class CandidateDetailView(LoginRequiredMixin, DetailView):
    model = Candidate
    template_name = "candidates/candidate_detail.html"
    context_object_name = "candidate"

    def get_queryset(self):
        return Candidate.objects.prefetch_related(
            "skills", "experiences", "education", "languages", "certifications", "documents"
        )


class ApplicationDetailView(LoginRequiredMixin, DetailView):
    model = Application
    template_name = "candidates/application_detail.html"
    context_object_name = "application"

    def get_queryset(self):
        return Application.objects.select_related("candidate", "offer", "document")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # L'historique des scores est conserve : on affiche le plus recent.
        context["score"] = self.object.scores.order_by("-created_at").first()
        return context
