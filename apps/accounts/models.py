from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Utilisateur RH.

    `blind_screening` active le masquage des attributs identitaires (nom,
    photo, nationalite, etablissement) dans les vues de tri, pour reduire les
    biais a la premiere lecture.
    """

    class Role(models.TextChoices):
        ADMIN = "admin", "Administrateur"
        RECRUITER = "recruiter", "Recruteur"
        HIRING_MANAGER = "hiring_manager", "Manager operationnel"
        VIEWER = "viewer", "Lecture seule"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.RECRUITER)
    department = models.CharField("departement", max_length=120, blank=True)
    blind_screening = models.BooleanField("screening a l'aveugle", default=False)

    class Meta:
        verbose_name = "utilisateur"
        verbose_name_plural = "utilisateurs"

    def __str__(self) -> str:
        return self.get_full_name() or self.username

    @property
    def can_decide(self) -> bool:
        """Seul un humain habilite tranche : l'IA classe, elle ne rejette jamais."""
        return self.role in {self.Role.ADMIN, self.Role.RECRUITER, self.Role.HIRING_MANAGER}
