from django import forms

from apps.jobs.models import JobOffer


class CVUploadForm(forms.Form):
    file = forms.FileField(
        label="CV (PDF ou DOCX)",
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.docx"}),
    )
    offer = forms.ModelChoiceField(
        label="Rattacher a une offre",
        queryset=JobOffer.objects.filter(status=JobOffer.Status.OPEN),
        required=False,
        empty_label="Aucune",
    )
