"""Interface en arabe, et lecture de droite a gauche.

L'application savait deja lire les CV en arabe — normalisation, formes de
presentation, ordre logique — mais son interface ne le parlait pas. Passer a
l'arabe n'est pas une affaire de mots : l'ecriture va de droite a gauche, et
c'est la mise en page qui change.

Ces tests portent sur trois choses : que le catalogue se compile sans gettext,
que la page se retourne, et que ce qui ne doit **pas** se retourner reste en
place.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from django.conf import settings
from django.core.management import call_command
from django.utils import translation

from apps.core.management.commands.compile_messages import ecrire_mo, lire_po

CSS = pathlib.Path(settings.BASE_DIR) / "static" / "css" / "app.css"
GABARITS = pathlib.Path(settings.BASE_DIR) / "templates"

# Proprietes qui figent un cote de l'ecran. En lecture de droite a gauche elles
# placent l'element du mauvais cote, en silence.
PHYSIQUES = re.compile(
    r"^\s*(?:margin|padding|border)-(?:left|right)\b"
    r"|text-align:\s*(?:left|right)\b",
    re.MULTILINE,
)


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


# --- Le catalogue ------------------------------------------------------------
def test_the_catalogue_compiles_without_gettext(tmp_path):
    """`compilemessages` appelle msgfmt, absent d'une machine Windows
    ordinaire. Demander une chaine d'outils C pour afficher une interface en
    arabe serait disproportionne."""
    source = tmp_path / "django.po"
    source.write_text(
        'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        'msgid "Tableau de bord"\nmsgstr "لوحة القيادة"\n',
        encoding="utf-8",
    )

    catalogue = lire_po(source)
    octets = ecrire_mo(catalogue, tmp_path / "django.mo")

    assert catalogue["Tableau de bord"] == "لوحة القيادة"
    assert octets > 0
    # Nombre magique du format, en petit-boutien.
    assert (tmp_path / "django.mo").read_bytes()[:4] == b"\xde\x12\x04\x95"


def test_gettext_reads_what_we_wrote(tmp_path):
    """Le vrai controle : ce n'est pas notre lecteur qui relit notre ecriture,
    c'est celui de Python."""
    import gettext as bibliotheque

    chemin = tmp_path / "django.mo"
    ecrire_mo({"": "Content-Type: text/plain; charset=UTF-8\n",
               "Candidats": "المترشحون",
               "Agent": "الوكيل"}, chemin)

    with chemin.open("rb") as fichier:
        catalogue = bibliotheque.GNUTranslations(fichier)

    assert catalogue.gettext("Candidats") == "المترشحون"
    assert catalogue.gettext("Agent") == "الوكيل"
    assert catalogue.gettext("Absent") == "Absent"


def test_an_untranslated_entry_is_not_written(tmp_path):
    """Une traduction vide signifie « pas encore traduit » : l'ecrire ferait
    disparaitre le texte d'origine au lieu de le laisser passer."""
    source = tmp_path / "django.po"
    source.write_text(
        'msgid "Traduit"\nmsgstr "مترجم"\n\nmsgid "Pas encore"\nmsgstr ""\n',
        encoding="utf-8",
    )

    catalogue = lire_po(source)

    assert "Traduit" in catalogue
    assert "Pas encore" not in catalogue


def test_the_parser_refuses_what_it_cannot_handle(tmp_path):
    """Un analyseur etroit qui avale le pluriel en silence produirait des
    traductions fausses sans prevenir."""
    source = tmp_path / "django.po"
    source.write_text(
        'msgid "un"\nmsgid_plural "plusieurs"\nmsgstr[0] "واحد"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="pluriel"):
        lire_po(source)


def test_the_project_catalogue_compiles(db):
    call_command("compile_messages")
    compile = pathlib.Path(settings.LOCALE_PATHS[0]) / "ar/LC_MESSAGES/django.mo"
    assert compile.exists()


def test_the_arabic_translation_applies():
    with translation.override("ar"):
        assert translation.gettext("Tableau de bord") == "لوحة القيادة"
        assert translation.get_language_bidi() is True

    with translation.override("fr"):
        assert translation.gettext("Tableau de bord") == "Tableau de bord"
        assert translation.get_language_bidi() is False


# --- La page se retourne -----------------------------------------------------
def test_the_page_declares_its_direction(client, recruteur):
    client.force_login(recruteur)

    francais = client.get("/").content.decode()
    assert 'lang="fr"' in francais
    assert 'dir="ltr"' in francais

    with translation.override("ar"):
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        arabe = client.get("/", headers={"accept-language": "ar"}).content.decode()

    assert 'lang="ar"' in arabe
    assert 'dir="rtl"' in arabe
    assert "لوحة القيادة" in arabe


def test_the_language_can_be_switched(client, recruteur):
    client.force_login(recruteur)

    reponse = client.post("/i18n/setlang/", {"language": "ar", "next": "/"})

    assert reponse.status_code == 302
    assert "لوحة القيادة" in client.get("/").content.decode()


def test_the_switcher_is_on_every_page(client, recruteur):
    client.force_login(recruteur)

    contenu = client.get("/").content.decode()

    assert 'action="/i18n/setlang/"' in contenu
    assert "العربية" in contenu


# --- Ce qui ne doit PAS se retourner -----------------------------------------
def test_the_stylesheet_holds_no_physical_direction():
    """Une propriete qui fige un cote de l'ecran place l'element du mauvais
    cote en lecture de droite a gauche, et le fait en silence."""
    fautives = PHYSIQUES.findall(CSS.read_text(encoding="utf-8"))

    assert not fautives, (
        "Utiliser les proprietes logiques : padding-inline-start, "
        f"border-inline-end, text-align: start/end. Trouve : {fautives}"
    )


def test_measurement_scales_stay_left_to_right():
    """Une jauge porte une grandeur de 0 a 100 %, en chiffres occidentaux. La
    retourner ferait voir les memes donnees en miroir a deux lecteurs de la
    meme page — et ce sont les donnees qui seraient mal lues."""
    contenu = CSS.read_text(encoding="utf-8")

    assert ".meter,\n.range,\n.chart {\n  direction: ltr;\n}" in contenu


def test_the_active_link_marker_is_flipped():
    """`box-shadow` n'a pas de variante logique : c'est la seule regle du
    fichier qui demande une inversion explicite."""
    contenu = CSS.read_text(encoding="utf-8")

    assert '[dir="rtl"] .nav-link.is-active' in contenu
    assert "inset -2px 0 0" in contenu


def test_no_template_hardcodes_a_direction():
    """Un `dir=` ecrit en dur dans une page annulerait la bascule.

    L'export PDF fait exception : il ecrit de l'arabe dans un document, ou le
    sens vient du contenu et non de la langue d'interface.
    """
    fautifs = []
    for gabarit in GABARITS.rglob("*.html"):
        contenu = gabarit.read_text(encoding="utf-8")
        for trouve in re.finditer(r'dir="(rtl|ltr)"', contenu):
            # La seule occurrence legitime est celle de base.html, calculee.
            if "{% if DROITE_A_GAUCHE %}" in contenu:
                continue
            fautifs.append(f"{gabarit.name}:{trouve.group(0)}")

    assert not fautifs, fautifs
