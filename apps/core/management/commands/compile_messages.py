"""Compile les catalogues de traduction, sans gettext installe.

    python manage.py compile_messages

`django-admin compilemessages` appelle `msgfmt`, un binaire de la suite
GNU gettext. Il n'est pas installe sur une machine Windows ordinaire, et
demander a qui clone le depot d'installer une chaine d'outils C pour afficher
une interface en arabe serait disproportionne.

Le format `.mo` tient en quelques dizaines de lignes : un en-tete, deux tables
de decalages, les chaines a la suite. On l'ecrit donc ici, en Python pur. C'est
le meme parti que partout ailleurs dans ce projet — BM25 ecrit a la main plutot
qu'un moteur de recherche, PyMuPDF pour ecrire les PDF plutot qu'une seconde
bibliotheque.

Ce qui n'est PAS fait ici : l'extraction des chaines depuis les gabarits, qui
demanderait `xgettext`. Les fichiers `.po` sont tenus a la main, et une commande
de controle signale les chaines traduites qui ont disparu du code.
"""

from __future__ import annotations

import array
import pathlib
import re
import struct

from django.conf import settings
from django.core.management.base import BaseCommand

# Nombre magique du format, en petit-boutien.
MAGIQUE = 0x950412DE

_ENTREE = re.compile(
    r'^(msgid|msgstr)\s+"(.*)"\s*$|^\s*"(.*)"\s*$', re.MULTILINE
)


def lire_po(chemin: pathlib.Path) -> dict[str, str]:
    """Extrait les couples (source, traduction) d'un fichier `.po`.

    Analyseur volontairement etroit : il couvre `msgid`/`msgstr`, les chaines
    poursuivies sur plusieurs lignes et les commentaires. Il ne couvre ni le
    pluriel ni le contexte, absents des catalogues de ce projet — et il le dit
    plutot que de les avaler en silence.
    """
    catalogue: dict[str, str] = {}
    cle = valeur = None
    courant = None

    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        depouillee = ligne.strip()
        if not depouillee or depouillee.startswith("#"):
            continue
        if depouillee.startswith(("msgid_plural", "msgctxt")):
            raise ValueError(
                f"{chemin.name} : pluriel ou contexte non gere par ce "
                f"compilateur, et absent des catalogues du projet."
            )

        if depouillee.startswith("msgid "):
            if cle is not None and valeur is not None:
                catalogue[cle] = valeur
            cle, courant = _contenu(depouillee, "msgid "), "id"
            valeur = None
        elif depouillee.startswith("msgstr "):
            valeur, courant = _contenu(depouillee, "msgstr "), "str"
        elif depouillee.startswith('"'):
            morceau = _dechapper(depouillee[1:-1])
            if courant == "id":
                cle = (cle or "") + morceau
            else:
                valeur = (valeur or "") + morceau

    if cle is not None and valeur is not None:
        catalogue[cle] = valeur
    # Une entree vide est la traduction de l'en-tete ; une traduction vide
    # signifie « pas encore traduit » et doit rester non traduite.
    return {
        source: traduction
        for source, traduction in catalogue.items()
        if traduction or source == ""
    }


def _contenu(ligne: str, prefixe: str) -> str:
    return _dechapper(ligne[len(prefixe):].strip()[1:-1])


def _dechapper(texte: str) -> str:
    return (
        texte.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\\\", "\\")
    )


def ecrire_mo(catalogue: dict[str, str], destination: pathlib.Path) -> int:
    """Ecrit le catalogue au format `.mo`. Renvoie le nombre d'octets.

    Structure : en-tete de 28 octets, table des sources, table des
    traductions, puis les chaines. Chaque table donne (longueur, decalage)
    pour chaque entree. Les cles sont triees — `gettext` fait une recherche
    dichotomique dessus.
    """
    entrees = sorted(catalogue.items())
    sources = [source.encode("utf-8") for source, _ in entrees]
    traductions = [traduction.encode("utf-8") for _, traduction in entrees]

    total = len(entrees)
    debut_tables = 28
    debut_sources = debut_tables + total * 8 * 2
    debut_textes = debut_sources

    table_sources, decalage = [], debut_textes
    for octets in sources:
        table_sources.append((len(octets), decalage))
        decalage += len(octets) + 1  # le zero terminal
    table_traductions = []
    for octets in traductions:
        table_traductions.append((len(octets), decalage))
        decalage += len(octets) + 1

    sortie = array.array("B")
    sortie.frombytes(
        struct.pack(
            "<Iiiiiii",
            MAGIQUE,
            0,  # version
            total,
            debut_tables,
            debut_tables + total * 8,
            0,  # taille de la table de hachage : aucune
            0,  # decalage de la table de hachage
        )
    )
    for longueur, position in table_sources + table_traductions:
        sortie.frombytes(struct.pack("<ii", longueur, position))
    for octets in sources + traductions:
        sortie.frombytes(octets + b"\x00")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(sortie.tobytes())
    return len(sortie)


class Command(BaseCommand):
    help = "Compile les .po en .mo sans dependre du binaire msgfmt."

    def handle(self, *args, **options):
        racines = [pathlib.Path(chemin) for chemin in settings.LOCALE_PATHS]
        compiles = 0

        for racine in racines:
            if not racine.exists():
                self.stdout.write(
                    self.style.WARNING(f"  {racine} : absent, rien a compiler")
                )
                continue
            for source in sorted(racine.glob("*/LC_MESSAGES/*.po")):
                catalogue = lire_po(source)
                destination = source.with_suffix(".mo")
                octets = ecrire_mo(catalogue, destination)
                traduites = sum(
                    1 for cle, valeur in catalogue.items() if cle and valeur
                )
                self.stdout.write(
                    f"  {source.parent.parent.name:<6} "
                    f"{traduites:>4} chaines  {octets:>6} octets  "
                    f"{destination.relative_to(racine)}"
                )
                compiles += 1

        if not compiles:
            self.stdout.write(self.style.WARNING("\nAucun catalogue trouve."))
            return
        self.stdout.write(
            self.style.SUCCESS(f"\n{compiles} catalogue(s) compile(s).")
        )
