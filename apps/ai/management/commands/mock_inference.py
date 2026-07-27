"""Lance le serveur d'inference factice.

    python manage.py mock_inference
    python manage.py mock_inference --port 30000 --fail-rate 0.3

Puis, dans un autre terminal, avec le .env pointant sur ce port :

    python manage.py check_ai
    python manage.py parse_cv chemin\\vers\\cv.pdf
    python manage.py score_offer <slug>

Sert a exercer la couche reseau quand le vrai serveur n'est pas joignable.
Les reponses sont fabriquees par des regles, pas par un modele : ce banc
d'essai valide la plomberie, jamais la qualite de l'extraction.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.ai.mock_server import MockConfig, MockInferenceServer


class Command(BaseCommand):
    help = "Serveur d'inference factice compatible OpenAI, pour tests locaux."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=30000)
        parser.add_argument(
            "--fail-rate", type=float, default=0.0, dest="fail_rate",
            help="Proportion d'appels repondant 503, pour exercer la reprise.",
        )
        parser.add_argument(
            "--reject-response-format", action="store_true",
            dest="reject_response_format",
            help="Simule un serveur ignorant response_format (repli guided_json).",
        )
        parser.add_argument(
            "--reasoning", action="store_true",
            help="Simule un modele a raisonnement (Qwen3) : reasoning_content "
                 "et troncature si max_tokens est trop court.",
        )
        parser.add_argument(
            "--latency", type=int, default=0, dest="latency_ms",
            help="Latence artificielle en millisecondes.",
        )
        parser.add_argument(
            "--model", action="append", dest="models",
            help="Identifiant de modele expose. Repetable.",
        )

    def handle(self, *args, **options):
        config = MockConfig(
            models=options["models"],
            fail_rate=options["fail_rate"],
            reject_response_format=options["reject_response_format"],
            reasoning=options["reasoning"],
            latency_ms=options["latency_ms"],
        )
        server = MockInferenceServer(config, port=options["port"]).start()

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n== Serveur d'inference factice ==")
        )
        self.stdout.write(f"Ecoute sur     {server.base_url}")
        self.stdout.write(f"Modeles        {', '.join(config.models)}")
        if config.fail_rate:
            self.stdout.write(
                self.style.WARNING(f"Taux d'echec   {config.fail_rate:.0%} (503)")
            )
        if config.reject_response_format:
            self.stdout.write(
                self.style.WARNING("response_format refuse : repli guided_json attendu")
            )
        self.stdout.write(
            self.style.WARNING(
                "\nCe n'est pas un modele. Les reponses sont fabriquees par des "
                "regles :\nelles valident la couche reseau, pas la qualite de "
                "l'extraction.\n"
            )
        )
        self.stdout.write("Ctrl+C pour arreter.\n")

        try:
            while True:
                import time

                time.sleep(3600)
        except KeyboardInterrupt:
            self.stdout.write("\nArret.")
        finally:
            server.stop()
