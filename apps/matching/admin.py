from django.contrib import admin

from .models import MatchScore


@admin.register(MatchScore)
class MatchScoreAdmin(admin.ModelAdmin):
    list_display = (
        "application", "percentage", "engine_version", "semantic_used",
        "compute_ms", "is_overridden", "created_at",
    )
    list_filter = ("engine_version", "semantic_used", "explanation_prompt_version")
    search_fields = ("application__candidate__full_name", "application__offer__title")
    date_hierarchy = "created_at"
    readonly_fields = (
        "application", "overall", "engine_version", "weights_used", "breakdown",
        "skill_matches", "gaps", "semantic_used", "compute_ms", "explanation",
        "explanation_prompt_id", "explanation_prompt_version", "explanation_model",
    )
    fieldsets = (
        ("Score calcule", {"fields": readonly_fields}),
        (
            "Correction humaine",
            {
                "fields": ("overridden_score", "overridden_by", "override_reason"),
                "description": (
                    "Le score calcule n'est jamais efface. Une correction est "
                    "imputee a son auteur et journalisee."
                ),
            },
        ),
    )

    def has_add_permission(self, request):
        return False
