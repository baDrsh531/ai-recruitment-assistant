from django.contrib import admin

from .models import RecruiterQuery


@admin.register(RecruiterQuery)
class RecruiterQueryAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "offer", "question", "matched_count",
        "has_rejected_criteria", "latency_ms",
    )
    list_filter = ("offer", "answer_prompt_version")
    search_fields = ("question", "answer")
    date_hierarchy = "created_at"
    readonly_fields = [field.name for field in RecruiterQuery._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
