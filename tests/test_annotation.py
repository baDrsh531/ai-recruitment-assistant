"""Faire annoter le jeu par quelqu'un d'autre.

La limite la plus serieuse des jeux annotes de ce projet n'est pas leur taille,
c'est qu'une seule personne les a ecrits. Ce module ne fait pas apparaitre un
second annotateur — il enleve ce qui l'empechait d'exister.

Ces tests portent surtout sur deux refus : ne pas montrer les notes existantes,
et ne pas publier un kappa qui ne veut rien dire.
"""

from __future__ import annotations

import csv
import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.evaluation import annotation, harness

JEU = {
    "cases": [
        {
            "id": "cas_a",
            "offer": {
                "title": "Backend",
                "required_skills": [{"name": "Python", "min_years": 3}],
            },
            "candidates": [
                {
                    "id": "a1", "name": "Alice", "relevance": 3, "years": 6,
                    "education": 5,
                    "skills": [{"name": "Python", "years": 6, "last_used_year": 2026}],
                    "languages": [{"language": "Francais", "level": "NAT"}],
                },
                {
                    "id": "a2", "name": "Bob", "relevance": 1, "years": 2,
                    "education": 3, "skills": [], "languages": [],
                },
            ],
        }
    ]
}


def _remplir(contenu: str, notes: dict[str, int]) -> str:
    """Simule un annotateur : remplit la colonne vide."""
    lignes = list(csv.DictReader(io.StringIO(contenu)))
    tampon = io.StringIO()
    graveur = csv.DictWriter(
        tampon, fieldnames=annotation.COLONNES, lineterminator="\n"
    )
    graveur.writeheader()
    for ligne in lignes:
        ligne["pertinence"] = notes.get(ligne["identifiant"], "")
        graveur.writerow(ligne)
    return tampon.getvalue()


# --- L'export ----------------------------------------------------------------
def test_the_sheet_never_shows_the_existing_notes():
    """Les montrer ferait du second annotateur un relecteur du premier, et
    l'accord mesure ne vaudrait plus rien."""
    contenu = annotation.exporter(JEU)

    for ligne in csv.DictReader(io.StringIO(contenu)):
        assert ligne["pertinence"] == "", f"{ligne['identifiant']} porte deja une note"


def test_the_sheet_carries_what_is_needed_to_judge():
    """Un annotateur qui ne connait pas le code doit pouvoir trancher : ce que
    l'offre exige, et ce que le profil apporte."""
    ligne = next(csv.DictReader(io.StringIO(annotation.exporter(JEU))))

    assert "Backend" in ligne["offre"]
    assert "Python" in ligne["offre"], "les exigences de l'offre"
    assert "Python" in ligne["competences"], "ce que le candidat apporte"
    assert ligne["anciennete"]


def test_a_profile_without_skills_is_still_exportable():
    """L'extraction echoue parfois : le cas doit s'exporter, pas casser."""
    contenu = annotation.exporter(JEU)
    lignes = {item["identifiant"]: item for item in csv.DictReader(io.StringIO(contenu))}

    assert lignes["a2"]["competences"] == "aucune"
    assert lignes["a2"]["langues"] == "aucune"


def test_every_candidate_of_the_real_dataset_is_exported(db):
    dataset = harness.load_dataset("ranking_v1")
    attendu = sum(len(cas["candidates"]) for cas in dataset["cases"])

    lignes = list(csv.DictReader(io.StringIO(annotation.exporter(dataset))))

    assert len(lignes) == attendu


# --- La comparaison ----------------------------------------------------------
def test_a_perfect_agreement_gives_a_kappa_of_one():
    rempli = _remplir(annotation.exporter(JEU), {"a1": 3, "a2": 1})

    accord = annotation.comparer(JEU, rempli)

    assert accord.communs == 2
    assert accord.identiques == 2
    assert accord.kappa == pytest.approx(1.0)
    assert accord.ecarts == []


def test_disagreements_are_named():
    """Un ecart n'est pas une erreur : c'est ce que le jeu ne disait pas."""
    rempli = _remplir(annotation.exporter(JEU), {"a1": 2, "a2": 1})

    accord = annotation.comparer(JEU, rempli)

    assert accord.ecarts == [("cas_a/a1", 3, 2)]
    assert accord.accord_brut == pytest.approx(0.5)


def test_an_unannotated_profile_is_reported_not_guessed():
    rempli = _remplir(annotation.exporter(JEU), {"a1": 3})

    accord = annotation.comparer(JEU, rempli)

    assert accord.communs == 1
    assert accord.manquants == ["cas_a/a2"]


def test_a_row_the_dataset_does_not_know_is_flagged():
    """Identifiants modifies entre l'export et le retour : le silence ferait
    croire a un accord sur des profils absents."""
    rempli = _remplir(annotation.exporter(JEU), {"a1": 3, "a2": 1})
    rempli = rempli.replace("a2", "inconnu")

    accord = annotation.comparer(JEU, rempli)

    assert accord.inconnus == ["cas_a/inconnu"]


@pytest.mark.parametrize("valeur", ["quatre", "7", "-1"])
def test_an_unreadable_note_is_refused(valeur):
    """Avaler une note illisible produirait un kappa faux sans rien signaler."""
    rempli = _remplir(annotation.exporter(JEU), {"a1": valeur})

    with pytest.raises(ValueError):
        annotation.comparer(JEU, rempli)


# --- Ce que le kappa refuse de dire ------------------------------------------
def test_a_kappa_on_too_few_profiles_is_not_published():
    """Sur cinq profils, un seul desaccord fait bouger le kappa de 0,2 a 0,8."""
    rempli = _remplir(annotation.exporter(JEU), {"a1": 3, "a2": 1})

    accord = annotation.comparer(JEU, rempli)

    assert not accord.suffisant
    assert "trop peu" in accord.lecture


def test_a_flat_annotator_makes_the_kappa_undefined():
    """Meme note partout : l'accord attendu au hasard vaut 1, le kappa est
    indefini. Rendre 0.0 se lirait « aucun accord », ce qui serait faux."""
    assert annotation.kappa_categoriel([2, 2, 2], [2, 2, 2]) is None
    assert annotation.kappa_categoriel([], []) is None


def test_the_binary_kappa_produces_nonsense_on_four_levels():
    """Pourquoi une fonction distincte etait necessaire.

    `agreement.cohen_kappa` attend des booleens et calcule `sum(a) / total` pour
    en tirer une proportion. Sur des entiers de 0 a 3, cette somme additionne
    les VALEURS : la « proportion » depasse 1, et le resultat n'a plus aucun
    sens. Reutiliser cette fonction aurait donne un chiffre faux sans rien
    signaler.
    """
    from apps.evaluation.agreement import cohen_kappa as binaire

    gauche, droite = [3, 2, 1, 0], [2, 3, 1, 0]

    assert binaire(gauche, droite) == 0.0, "chiffre sans rapport avec l'accord reel"
    categoriel = annotation.kappa_categoriel(gauche, droite)
    assert 0.0 < categoriel < 1.0, "deux profils sur quatre concordent vraiment"


def test_the_categorical_kappa_falls_below_raw_agreement():
    """Le kappa retire l'accord du au hasard : sur des notes tres desequilibrees,
    il doit etre nettement inferieur au pourcentage brut."""
    gauche = [3] * 8 + [0, 0]
    droite = [3] * 7 + [0, 3, 0]

    kappa = annotation.kappa_categoriel(gauche, droite)
    brut = sum(1 for a, b in zip(gauche, droite, strict=True) if a == b) / len(gauche)

    assert brut > 0.7, "l'accord brut flatte"
    assert kappa < brut, "le kappa doit retirer ce que le hasard explique"


# --- La commande --------------------------------------------------------------
def test_the_command_exports_and_compares(db, tmp_path, capsys):
    fichier = tmp_path / "annotation.csv"
    call_command("annotate", "--export", str(fichier))
    assert fichier.is_file()

    sortie = capsys.readouterr().out
    assert "132" in sortie, "le nombre de profils a annoter"
    assert "relecteur du premier" in sortie, "la raison de la colonne vide"

    dataset = harness.load_dataset("ranking_v1")
    reference = annotation.annotations_du_jeu(dataset)
    rempli = _remplir(
        fichier.read_text(encoding="utf-8-sig"),
        {cle.split("/", 1)[1]: valeur for cle, valeur in reference.items()},
    )
    retour = tmp_path / "retour.csv"
    retour.write_text(rempli, encoding="utf-8")

    call_command("annotate", "--comparer", str(retour))

    sortie = capsys.readouterr().out
    assert "kappa de Cohen" in sortie
    assert "1.00" in sortie, "annotations identiques au jeu"


def test_the_command_needs_a_verb(db):
    with pytest.raises(CommandError, match="export"):
        call_command("annotate")


def test_the_command_refuses_a_missing_file(db, tmp_path):
    with pytest.raises(CommandError, match="introuvable"):
        call_command("annotate", "--comparer", str(tmp_path / "absent.csv"))
