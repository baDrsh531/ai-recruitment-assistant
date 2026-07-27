"""Diagnostic de la couche IA.

    python manage.py check_ai

Verifie que les deux endpoints repondent, liste les identifiants exacts des
modeles exposes (a recopier dans le .env), teste le decodage contraint par
JSON Schema et mesure la latence reelle.
"""

from __future__ import annotations

import time

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.ai import embeddings
from apps.ai.client import InferenceClient, InferenceError, chat_client, vision_client

OK = "  [OK]  "
KO = "  [KO]  "
WARN = "  [!]   "

PING_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "country": {"type": "string"},
    },
}


class Command(BaseCommand):
    help = "Verifie la connectivite et la configuration du serveur d'inference."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-embeddings",
            action="store_true",
            help="Ne pas tester les embeddings (evite le telechargement du modele local).",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("\n== Couche IA =="))
        failures = 0

        failures += self._check("Texte  ", settings.LLM, chat_client)
        failures += self._check("Vision ", settings.VLM, vision_client)

        if not options["skip_embeddings"]:
            failures += self._check_embeddings()

        self.stdout.write("")
        if failures:
            self.stdout.write(
                self.style.ERROR(f"{failures} verification(s) en echec. Voir ci-dessus.\n")
            )
        else:
            self.stdout.write(self.style.SUCCESS("Tout est operationnel.\n"))

    # ----------------------------------------------------------------------
    def _check(self, label: str, config: dict, factory) -> int:
        try:
            client = factory()
        except InferenceError as exc:
            self.stdout.write(f"\n{label} non configure")
            self.stdout.write(KO + self.style.ERROR(str(exc)))
            return 1
        return self._check_endpoint(label, config, client)

    def _check_endpoint(self, label: str, config: dict, client: InferenceClient) -> int:
        base_url = config["BASE_URL"]
        configured = config["MODEL"]
        self.stdout.write(f"\n{label} {base_url}")

        try:
            models = client.list_models()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            self.stdout.write(KO + self.style.ERROR(f"/models injoignable : {exc}"))
            self.stdout.write(
                WARN + "Verifie que le serveur tourne et que la machine est sur le "
                "meme reseau (les NodePort k8s ne sont pas exposes hors du cluster)."
            )
            return 1

        self.stdout.write(OK + f"/models repond — {len(models)} modele(s) :")
        for name in models:
            marker = "  <- configure" if name == configured else ""
            self.stdout.write(f"          {name}{marker}")

        if configured not in models:
            self.stdout.write(
                WARN + self.style.WARNING(
                    f"'{configured}' n'est pas dans la liste. Recopie l'identifiant "
                    "exact ci-dessus dans le .env."
                )
            )

        # Budget large : un modele a raisonnement peut consommer plusieurs
        # centaines de tokens avant de commencer sa reponse.
        try:
            started = time.perf_counter()
            response = client.chat(
                [{"role": "user", "content": "Capitale de la France ? Reponds en JSON."}],
                schema=PING_SCHEMA,
                schema_name="ville",
                max_tokens=1024,
                purpose="healthcheck",
                record=False,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
        except InferenceError as exc:
            self.stdout.write(KO + self.style.ERROR(f"Appel de test echoue : {exc}"))
            return 1

        self.stdout.write(
            OK + f"Decodage contraint JSON Schema fonctionnel — {response.parsed} "
            f"({elapsed} ms, {response.completion_tokens} tokens generes)"
        )

        # Compare le cout avec et sans raisonnement : sur un modele qui en a un,
        # l'ecart est le principal levier de cout du projet.
        try:
            thinking = client.chat(
                [{"role": "user", "content": "Capitale de la France ? Reponds en JSON."}],
                schema=PING_SCHEMA, schema_name="ville", max_tokens=2048,
                thinking=True, purpose="healthcheck", record=False,
            )
        except InferenceError:
            return 0

        if thinking.reasoning:
            factor = thinking.completion_tokens / max(response.completion_tokens, 1)
            self.stdout.write(
                OK + f"Modele a raisonnement detecte — {thinking.completion_tokens} "
                f"tokens avec reflexion contre {response.completion_tokens} sans "
                f"({factor:.0f}x)."
            )
            self.stdout.write(
                "          Le projet desactive le raisonnement par defaut : "
                "il ne change pas le resultat d'une extraction structuree."
            )
        return 0

    def _check_embeddings(self) -> int:
        config = settings.EMBEDDING
        self.stdout.write(f"\nEmbeddings  provider={config['PROVIDER']} modele={config['MODEL']}")
        try:
            started = time.perf_counter()
            embedder = embeddings.get_embedder()
            vectors = embedder.encode(
                ["Ingenieur backend Python Django", "Developpeur Python et API REST"]
            )
            elapsed = int((time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(KO + self.style.ERROR(str(exc)))
            return 1

        similarity = embeddings.cosine(vectors[0], vectors[1])
        self.stdout.write(OK + f"Dimension {vectors.shape[1]} ({elapsed} ms, chargement inclus)")
        self.stdout.write(f"          similarite de controle : {similarity:.3f} (attendu > 0.7)")

        if vectors.shape[1] != config["DIM"]:
            self.stdout.write(
                WARN + self.style.WARNING(
                    f"EMBEDDING_DIM={config['DIM']} mais le modele renvoie "
                    f"{vectors.shape[1]}. Corrige le .env."
                )
            )
        return 0
