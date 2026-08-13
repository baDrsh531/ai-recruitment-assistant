from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import FormView, ListView

from apps.candidates.models import CVDocument
from apps.core.permissions import ActionPermissionMixin

from .forms import CVUploadForm
from .services import ingest


class CVUploadView(ActionPermissionMixin, LoginRequiredMixin, FormView):
    template_name = "parsing/upload.html"
    form_class = CVUploadForm
    success_url = reverse_lazy("parsing:documents")

    def form_valid(self, form):
        try:
            document, created = ingest(
                form.cleaned_data["file"],
                offer=form.cleaned_data.get("offer"),
                actor=self.request.user,
                request=self.request,
            )
        except ValidationError as exc:
            form.add_error("file", exc)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            messages.error(self.request, f"Extraction impossible : {exc}")
            return redirect(self.success_url)

        if not created:
            messages.info(
                self.request, "Ce CV avait deja ete traite ; l'extraction a ete reutilisee."
            )
        elif document.status == CVDocument.Status.DONE:
            messages.success(
                self.request,
                f"CV extrait via « {document.get_method_display()} » "
                f"en {document.extraction_seconds:.1f} s.",
            )
        else:
            messages.info(self.request, "CV depose, extraction en cours.")

        if document.candidate_id:
            return redirect(document.candidate.get_absolute_url())
        return redirect(self.success_url)


class CVDocumentListView(LoginRequiredMixin, ListView):
    model = CVDocument
    template_name = "parsing/document_list.html"
    context_object_name = "documents"
    paginate_by = 30

    def get_queryset(self):
        return CVDocument.objects.select_related("candidate").order_by("-created_at")

    def get_context_data(self, **kwargs):
        # Cette page affichait le nom du candidat et le nom du fichier sans
        # tenir compte du screening a l'aveugle. Or un CV s'appelle presque
        # toujours « Prenom Nom.pdf » : l'attenuation du biais etait annulee
        # par la liste des depots, une page en apparence anodine.
        context = super().get_context_data(**kwargs)
        context["blind"] = self.request.user.blind_screening
        return context
