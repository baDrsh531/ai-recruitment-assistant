from django.contrib import admin

from .models import JobLanguage, JobOffer, JobSkill


class JobSkillInline(admin.TabularInline):
    model = JobSkill
    extra = 3
    fields = ("name", "requirement", "weight", "min_years")


class JobLanguageInline(admin.TabularInline):
    model = JobLanguage
    extra = 1


@admin.register(JobOffer)
class JobOfferAdmin(admin.ModelAdmin):
    list_display = (
        "title", "department", "location", "status", "blind_screening", "created_at",
    )
    list_filter = ("status", "department", "contract_type", "remote_policy", "blind_screening")
    search_fields = ("title", "description", "department")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [JobSkillInline, JobLanguageInline]
    fieldsets = (
        (None, {"fields": ("title", "slug", "description", "status")}),
        ("Poste", {"fields": ("department", "location", "remote_policy", "contract_type")}),
        ("Remuneration", {"fields": ("salary_min", "salary_max", "currency")}),
        (
            "Exigences",
            {
                "fields": (
                    "experience_min_years", "experience_max_years",
                    "education_level", "required_certifications", "deadline",
                )
            },
        ),
        (
            "Scoring",
            {
                "fields": ("scoring_weights", "blind_screening"),
                "description": (
                    "Laisser la ponderation vide pour utiliser celle par defaut. "
                    "Le screening a l'aveugle exclut la localisation du calcul : "
                    "voir la page Transparence pour l'effet mesure."
                ),
            },
        ),
    )

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
