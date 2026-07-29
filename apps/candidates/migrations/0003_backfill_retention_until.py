"""Donne une echeance de conservation aux dossiers deja en base.

`Candidate.save()` fixe desormais `retention_until`, mais seulement a l'ecriture.
Les dossiers crees avant ce changement gardaient un champ vide, et la purge les
ignore silencieusement : filtrer sur `retention_until__lt` ne selectionne jamais
un NULL. Ils auraient donc ete conserves indefiniment — exactement ce que la
purge est censee empecher. Leur echeance est calculee depuis leur date de
creation reelle, pas depuis aujourd'hui : un dossier vieux d'un an ne repart pas
pour une duree complete.
"""

import datetime as dt

from django.conf import settings
from django.db import migrations


def dater_les_dossiers_existants(apps, schema_editor):
    Candidate = apps.get_model("candidates", "Candidate")
    duree = dt.timedelta(days=settings.DATA_RETENTION_DAYS)
    for candidat in Candidate.objects.filter(retention_until__isnull=True):
        candidat.retention_until = candidat.created_at.date() + duree
        candidat.save(update_fields=["retention_until"])


def revenir_en_arriere(apps, schema_editor):
    # Rien a defaire : effacer les echeances rendrait les dossiers impurgeables.
    pass


class Migration(migrations.Migration):
    dependencies = [("candidates", "0002_evidencespan_match_ratio")]

    operations = [
        migrations.RunPython(dater_les_dossiers_existants, revenir_en_arriere),
    ]
