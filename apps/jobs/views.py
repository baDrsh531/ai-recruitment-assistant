from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import DetailView, ListView

from .models import JobOffer


class JobOfferListView(LoginRequiredMixin, ListView):
    model = JobOffer
    template_name = "jobs/offer_list.html"
    context_object_name = "offers"
    paginate_by = 20

    def get_queryset(self):
        return JobOffer.objects.prefetch_related("skills").order_by("-created_at")


class JobOfferDetailView(LoginRequiredMixin, DetailView):
    model = JobOffer
    template_name = "jobs/offer_detail.html"
    context_object_name = "offer"

    def get_queryset(self):
        return JobOffer.objects.prefetch_related("skills", "languages")
