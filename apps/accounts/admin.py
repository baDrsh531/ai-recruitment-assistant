from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "role", "department", "is_active")
    list_filter = ("role", "department", "is_active", "is_staff")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Profil RH", {"fields": ("role", "department", "blind_screening")}),
    )
