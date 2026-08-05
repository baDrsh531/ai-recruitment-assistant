"""Fait tourner l'agent d'orchestration sur les dossiers en attente.

    python manage.py run_agent
    python manage.py run_agent --limit 5
    python manage.py run_agent --dry-run    # ce qui serait fait, sans le faire

L'agent prepare : il score, redige l'analyse, genere les questions et ecrit une
recommandation. **Il ne fait avancer aucune candidature.** Trancher reste
l'affaire d'un recruteur habilite, depuis l'interface.

Sans broker Celery, c'est cette commande qui fait tout le travail — a la main
ou par une tache periodique.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.agent import budget, pipeline
from apps.agent.models import AgentRun


class Command(BaseCommand):
    help = "Prepare les dossiers en attente. Ne tranche aucune candidature."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Affiche ce qui serait fait, sans rien executer.",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("\n== Agent d'orchestration =="))

        etat = budget.actuel()
        self.stdout.write(
            f"  interrupteur : {'actif' if budget.agent_actif() else 'COUPE'}"
        )
        self.stdout.write(
            "  budget       : "
            + (
                "illimite"
                if etat.illimite
                else f"{etat.consomme} / {etat.limite} tokens "
                f"({etat.part_consommee * 100:.0f} %)"
            )
        )

        if not budget.agent_actif():
            self.stdout.write(
                self.style.WARNING(
                    "\nL'agent est desactive. Mettre AGENT_ENABLED=True pour "
                    "l'autoriser a appeler le modele."
                )
            )
            return

        en_attente = list(pipeline.a_traiter())
        if options["limit"]:
            en_attente = en_attente[: options["limit"]]

        self.stdout.write(f"\n  {len(en_attente)} dossier(s) en attente")
        if options["dry_run"]:
            for application in en_attente:
                restantes = [
                    etape.libelle
                    for etape in pipeline.ETAPES
                    if not etape.faite(application)
                ]
                self.stdout.write(
                    f"    {application.candidate.full_name:<24}"
                    + (", ".join(restantes) or "rien a faire")
                )
            self.stdout.write("\nRien n'a ete execute (--dry-run).")
            return

        resultat = pipeline.run(
            applications=en_attente, trigger=AgentRun.Trigger.MANUAL
        )
        execution = resultat.run

        self.stdout.write(self.style.MIGRATE_HEADING("\n== Resultat =="))
        self.stdout.write(f"  statut          {execution.get_status_display()}")
        self.stdout.write(
            f"  dossiers        {execution.applications_processed} traites "
            f"sur {execution.applications_seen}"
        )
        self.stdout.write(f"  etapes          {execution.steps_done} executees, "
                          f"{execution.steps_failed} en echec")
        self.stdout.write(f"  recommandations {execution.recommendations_made}")
        self.stdout.write(f"  tokens          {execution.tokens_used}")
        self.stdout.write(f"  duree           {execution.duration_ms} ms")

        for ligne in resultat.journal:
            self.stdout.write(self.style.WARNING(f"    {ligne}"))

        if resultat.arrete_par_le_budget:
            self.stdout.write(
                self.style.ERROR(
                    "\nArrete par le budget. Les dossiers restants seront repris "
                    "a la prochaine execution, sans refaire ce qui est fait."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{execution.recommendations_made} recommandation(s) en attente "
                "d'un recruteur. Aucune candidature n'a avance."
            )
        )
