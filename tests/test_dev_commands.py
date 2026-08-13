"""Les deux commandes d'outillage, restees a 0 % de couverture.

`mock_inference` demarre un serveur, `probe_semantic` exige une couche
d'embeddings desactivee par defaut. Aucune n'etait exercee — et les deux sont
citees dans le README comme le moyen de travailler sans serveur d'inference et
de reproduire la mesure qui a fait desactiver le rapprochement semantique.

Une commande d'outillage cassee ne casse pas la production. Elle casse la
capacite de quelqu'un a reproduire ce que le projet affirme, ce qui est le
coeur de sa these.

Aucun de ces tests n'appelle un vrai modele.
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai.mock_server import MockConfig, MockInferenceServer


def _poster(url: str, charge: dict, delai: float = 5.0) -> tuple[int, dict]:
    """Appel HTTP direct, sans passer par le client du projet."""
    requete = urllib.request.Request(
        url,
        data=json.dumps(charge).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(requete, timeout=delai) as reponse:
            return reponse.status, json.loads(reponse.read())
    except urllib.error.HTTPError as erreur:
        return erreur.code, {}


# --- Le serveur factice -------------------------------------------------------
def test_the_mock_server_answers_like_an_openai_endpoint():
    """Ce banc d'essai existe pour exercer la couche reseau quand le vrai
    serveur n'est pas joignable. S'il ne repond pas au protocole, il ne valide
    rien."""
    with MockInferenceServer(MockConfig()) as serveur:
        statut, corps = _poster(
            f"{serveur.base_url}/chat/completions",
            {"model": "essai", "messages": [{"role": "user", "content": "bonjour"}]},
        )

    assert statut == 200
    assert corps["choices"][0]["message"]["content"]
    assert corps["model"]


def test_the_mock_server_lists_its_models():
    config = MockConfig(models=["mock-a", "mock-b"])
    with MockInferenceServer(config) as serveur:
        url = f"{serveur.base_url}/models"
        with urllib.request.urlopen(url, timeout=5) as reponse:  # noqa: SIM117
            corps = json.loads(reponse.read())

    identifiants = [item["id"] for item in corps["data"]]
    assert identifiants == ["mock-a", "mock-b"]


def test_a_failure_rate_of_one_always_answers_503():
    """Le mode degrade sert a eprouver la reprise du client. A 100 %, il doit
    echouer a tous les coups — sinon le test de reprise passe par chance."""
    with MockInferenceServer(MockConfig(fail_rate=1.0)) as serveur:
        statuts = [
            _poster(
                f"{serveur.base_url}/chat/completions",
                {"model": "x", "messages": [{"role": "user", "content": "a"}]},
            )[0]
            for _ in range(3)
        ]

    assert statuts == [503, 503, 503]


def test_rejecting_response_format_forces_the_fallback():
    """Certains serveurs ignorent `response_format`. Le client bascule alors sur
    `guided_json` ; encore faut-il pouvoir simuler ce refus."""
    charge = {
        "model": "x",
        "messages": [{"role": "user", "content": "a"}],
        "response_format": {"type": "json_schema"},
    }
    with MockInferenceServer(MockConfig(reject_response_format=True)) as serveur:
        statut, _ = _poster(f"{serveur.base_url}/chat/completions", charge)

    assert statut == 400, "le refus doit etre explicite, pas silencieux"


# --- La commande qui l'expose -------------------------------------------------
def test_the_mock_inference_command_starts_and_stops(monkeypatch, capsys):
    """La commande tourne jusqu'a un Ctrl+C. On neutralise l'attente pour
    exercer le demarrage, l'affichage et l'arret sans bloquer la suite."""
    import time

    def _interrompre(_secondes):
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", _interrompre)

    call_command("mock_inference", "--port", "0")

    sortie = capsys.readouterr().out
    assert "Serveur d'inference factice" in sortie
    assert "127.0.0.1" in sortie
    assert "Arret" in sortie


def test_the_command_announces_its_degraded_modes(monkeypatch, capsys):
    """Un banc d'essai qui echoue une fois sur trois sans le dire ferait
    chercher un defaut la ou il n'y en a pas."""
    import time

    monkeypatch.setattr(
        time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt)
    )

    call_command(
        "mock_inference", "--port", "0", "--fail-rate", "0.3",
        "--reject-response-format",
    )

    sortie = capsys.readouterr().out
    assert "30" in sortie, "le taux d'echec doit etre annonce"
    assert "guided_json" in sortie


def test_the_command_says_it_is_not_a_model(monkeypatch, capsys):
    """L'avertissement qui evite le pire malentendu : prendre les sorties de ce
    banc pour une mesure de qualite d'extraction."""
    import time

    monkeypatch.setattr(
        time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt)
    )

    call_command("mock_inference", "--port", "0")

    assert "n'est pas un modele" in capsys.readouterr().out


# --- La sonde du rapprochement semantique -------------------------------------
def test_probe_semantic_refuses_clearly_when_no_provider_is_installed(
    db, settings, monkeypatch
):
    """C'est l'etat par defaut du projet : la couche est desactivee, mesure a
    l'appui. La commande doit le dire et donner la marche a suivre, pas lever
    une trace."""
    from apps.ai import embeddings

    monkeypatch.setattr(embeddings, "get_embedder_or_none", lambda **k: None)

    with pytest.raises(CommandError) as erreur:
        call_command("probe_semantic")

    message = str(erreur.value)
    assert "Aucun fournisseur" in message
    assert "pip install" in message, "la commande doit dire comment l'installer"


def test_probe_semantic_measures_the_reference_pairs(db, monkeypatch, capsys):
    """La commande qui a fait desactiver le rapprochement semantique. Sans
    fournisseur reel, on en simule un : ce qu'on eprouve ici est la sonde, pas
    le modele d'embeddings."""
    import numpy

    from apps.ai import embeddings
    from apps.matching.management.commands import probe_semantic

    class _Faux:
        """Vecteurs deterministes, tires du texte : deux intitules identiques
        se ressemblent, deux differents beaucoup moins."""

        def encode(self, textes):
            vecteurs = []
            for texte in textes:
                graine = sum(ord(c) for c in texte.lower() if c.isalpha())
                rng = numpy.random.default_rng(graine)
                vecteur = rng.normal(size=32)
                vecteurs.append(vecteur / numpy.linalg.norm(vecteur))
            return numpy.array(vecteurs)

    monkeypatch.setattr(embeddings, "get_embedder_or_none", lambda **k: _Faux())

    call_command("probe_semantic")

    sortie = capsys.readouterr().out
    assert "Rapprochement semantique" in sortie
    assert "cosinus" in sortie
    # Chaque paire de reference doit apparaitre dans le releve.
    assert len(probe_semantic.PAIRES) >= 5
    for gauche, _, _ in probe_semantic.PAIRES[:3]:
        assert gauche in sortie
