from django.contrib import admin

from .models import (
    Application,
    Candidate,
    CandidateLanguage,
    CandidateSkill,
    Certification,
    CVDocument,
    Education,
    EvidenceSpan,
    Experience,
)


class SkillInline(admin.TabularInline):
    model = CandidateSkill
    extra = 0
    fields = ("name", "source", "years", "last_used_year", "confidence")


class ExperienceInline(admin.TabularInline):
    model = Experience
    extra = 0
    fields = ("title", "company", "start_date", "end_date", "confidence")


class EducationInline(admin.TabularInline):
    model = Education
    extra = 0


class LanguageInline(admin.TabularInline):
    model = CandidateLanguage
    extra = 0


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "headline", "total_experience_years", "location")
    search_fields = ("full_name", "email", "headline")
    list_filter = ("highest_education",)
    inlines = [SkillInline, ExperienceInline, EducationInline, LanguageInline]


@admin.register(CVDocument)
class CVDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "original_filename", "candidate", "status", "method",
        "page_count", "extraction_seconds", "created_at",
    )
    list_filter = ("status", "method")
    search_fields = ("original_filename", "content_hash")
    readonly_fields = ("content_hash", "raw_text", "quality", "size_bytes", "page_count")


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("candidate", "offer", "stage", "applied_at", "decided_by")
    list_filter = ("stage", "offer")
    search_fields = ("candidate__full_name", "offer__title")
    autocomplete_fields = ("candidate",)


admin.site.register([EvidenceSpan, Certification])

admin.site.site_header = "Assistant IA de recrutement"
admin.site.site_title = "Assistant IA de recrutement"
admin.site.index_title = "Administration"
