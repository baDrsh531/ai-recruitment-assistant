from django.contrib import admin

from .models import AIInvocation


@admin.register(AIInvocation)
class AIInvocationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "purpose", "kind", "model", "prompt_version",
        "latency_ms", "total_tokens", "status",
    )
    list_filter = ("kind", "purpose", "status", "model")
    search_fields = ("input_hash", "subject_id", "error")
    date_hierarchy = "created_at"
    readonly_fields = [field.name for field in AIInvocation._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
