"""La marque, appliquee partout de la meme facon.

Une identite recopiee a la main dans chaque sortie derive : l'ecran dit une
chose, le PDF une autre, le courriel une troisieme. Ces tests verifient que les
trois puisent au meme endroit, et surtout que la version HTML d'un courriel ne
raconte jamais autre chose que le texte enregistre.
"""

from __future__ import annotations

import re

import pytest
from django.core import mail
from django.template.loader import render_to_string

from apps.candidates.models import Application, Candidate
from apps.core import brand
from apps.evaluation import report_pdf
from apps.jobs.models import JobOffer, JobSkill
from apps.outreach import backends, drafting, services


@pytest.fixture(autouse=True)
def sans_modele(monkeypatch):
    monkeypatch.setattr(drafting, "personnaliser", lambda *a, **k: {})


@pytest.fixture(autouse=True)
def courrier_en_memoire(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    return settings


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter",
        first_name="Nadia", last_name="Cherkaoui",
    )


@pytest.fixture
def candidature(db):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    candidat = Candidate.objects.create(
        full_name="Sara El Amrani", email="sara@example.com", total_experience_years=4
    )
    return Application.objects.create(candidate=candidat, offer=offre)


# --- La source unique --------------------------------------------------------
def test_the_mark_is_rendered_from_the_svg_not_from_a_stored_png():
    """Un PNG range a cote du SVG serait une occasion de les laisser diverger."""
    png = brand.marque_png(taille=64)

    assert png.startswith(b"\x89PNG")
    assert len(png) > 200


def test_the_mark_follows_the_surrounding_colour():
    """`currentColor` est ce qui permet un seul fichier pour fond clair et
    fond sombre."""
    assert "currentColor" in brand.marque_svg()
    assert "currentColor" not in brand.marque_svg_colore("#ffffff")
    assert "#ffffff" in brand.marque_svg_colore("#ffffff")


def test_the_mark_carries_no_text():
    """« CV » et « Offre » etaient lisibles en grand et formaient une bouillie
    a 16 px, taille a laquelle une favicon passe le plus clair de son temps."""
    svg = brand.marque_svg()

    assert "<text" not in svg
    assert ">CV<" not in svg


def test_two_inks_give_two_different_images():
    clair = brand.marque_png(taille=48, encre="#131922")
    sombre = brand.marque_png(taille=48, encre="#ffffff")

    assert clair != sombre


def test_the_organisation_falls_back_to_the_brand_name(settings):
    settings.OUTREACH_ORGANISATION = ""
    assert brand.organisation() == brand.NOM

    settings.OUTREACH_ORGANISATION = "OCP Group"
    assert brand.organisation() == "OCP Group"


# --- Le courriel -------------------------------------------------------------
def test_the_email_carries_both_a_text_and_an_html_part(candidature, recruteur):
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )

    services.envoyer(message, actor=recruteur)

    courrier = mail.outbox[0]
    assert courrier.body == message.body
    assert [type_ for _, type_ in courrier.alternatives] == ["text/html"]


def test_the_html_says_nothing_the_text_does_not(candidature, recruteur):
    """Un HTML qui raconterait autre chose que le texte enregistre rendrait le
    journal faux : c'est le texte qui est relu, stocke et audite."""
    message = services.rediger(
        candidature, modele_id="invitation_entretien", actor=recruteur
    )
    services.envoyer(message, actor=recruteur)
    courrier = mail.outbox[0]

    html_en_texte = re.sub(r"<[^>]+>", " ", courrier.alternatives[0][0]).lower()
    mots_du_texte = set(re.findall(r"\w{4,}", courrier.body.lower()))
    mots_du_html = set(re.findall(r"\w{4,}", html_en_texte))

    assert mots_du_texte <= mots_du_html


def test_the_logo_travels_with_the_message(candidature, recruteur):
    """Une image chargee depuis une adresse distante est bloquee par defaut et
    trahit l'ouverture du message."""
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )
    services.envoyer(message, actor=recruteur)
    brut = mail.outbox[0].message().as_string()

    assert mail.outbox[0].mixed_subtype == "related"
    assert f"<{brand.CID_MARQUE}>" in brut
    assert "Content-Disposition: inline" in brut
    assert "http://" not in mail.outbox[0].alternatives[0][0]


def test_no_svg_ever_reaches_a_mailbox(candidature, recruteur):
    """Gmail et Outlook retirent les SVG : la marque part en PNG."""
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )
    services.envoyer(message, actor=recruteur)

    assert "<svg" not in mail.outbox[0].message().as_string()


def test_a_wrapped_paragraph_flows_again_in_html():
    """Les gabarits sont replies pour un courriel texte. Garder ces coupures en
    HTML figerait la mise en page quelle que soit la largeur de l'ecran."""
    bloc = (
        "Votre profil a retenu notre attention pour le poste de Data Engineer. Nous\n"
        "souhaiterions echanger avec vous."
    )

    assert backends._lignes(bloc) == [
        "Votre profil a retenu notre attention pour le poste de Data Engineer. "
        "Nous souhaiterions echanger avec vous."
    ]


def test_a_signature_keeps_its_line_breaks():
    """Toutes les fins de ligne ne sont pas des replis : le depart se fait sur
    la longueur, comme le format=flowed du courrier electronique."""
    bloc = "Bien cordialement,\nNadia Cherkaoui\nRecrutement.IA"

    assert backends._lignes(bloc) == [
        "Bien cordialement,", "Nadia Cherkaoui", "Recrutement.IA",
    ]


def test_the_html_email_only_uses_inline_styles():
    """Gmail retire les blocs `<style>` ; une feuille externe ne partirait pas."""
    html = backends.habiller("Bonjour,\n\nUn message court.")

    assert "<style" not in html
    assert "<link" not in html
    assert 'style="' in html


def test_the_email_layout_declares_a_bounded_width():
    """Au-dela de 600 px les clients coupent."""
    html = backends.habiller("Bonjour,\n\nUn message court.")

    assert 'width="600"' in html
    assert "max-width:600px" in html
    # Disposition en tableau : seule construction que le rendu d'Outlook honore.
    assert "<table" in html


def test_the_email_discloses_the_sorting_tool(candidature, recruteur):
    """La mention n'est pas decorative : elle dit au candidat qu'un outil
    intervient, et qu'il ne decide pas."""
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )
    services.envoyer(message, actor=recruteur)

    html = mail.outbox[0].alternatives[0][0]
    assert "il ne decide pas" in html


# --- Le PDF ------------------------------------------------------------------
def test_the_pdf_carries_the_mark_on_its_first_page(candidature, recruteur):
    import fitz

    octets = report_pdf.build_application(candidature, author="Nadia Cherkaoui")
    document = fitz.open(stream=octets, filetype="pdf")

    texte = report_pdf.texte_normalise(document[0].get_text())
    assert brand.NOM_RACINE in texte
    assert brand.NOM_SUFFIXE.lstrip(".") in texte
    # Le bandeau pose une image ; sans elle il ne resterait qu'un titre.
    assert document[0].get_images(), "la marque doit etre presente en image"


def test_the_mark_appears_on_every_page_not_only_the_first():
    """Une page de rapport se photocopie et se transfere seule : elle doit dire
    d'ou elle vient sans le reste du document."""
    import fitz

    document = fitz.open()
    redacteur = report_pdf._Redacteur(document)
    redacteur.bandeau_de_marque()
    for _ in range(3):
        redacteur.bloc("<p>" + ("texte de remplissage. " * 400) + "</p>")
    redacteur.pieds_de_page("Recrutement.IA")

    assert document.page_count > 1
    for page in document:
        assert page.get_images(), f"page {page.number + 1} sans marque"


def test_the_brand_header_pushes_the_content_down():
    """Sans avance du curseur, le titre s'ecrirait par-dessus la marque."""
    import fitz

    document = fitz.open()
    redacteur = report_pdf._Redacteur(document)
    depart = redacteur.y

    redacteur.bandeau_de_marque()

    assert redacteur.y == depart + redacteur.BANDEAU


# --- L'interface -------------------------------------------------------------
def test_the_inline_logo_uses_current_colour():
    """Une balise `<img>` resoudrait `currentColor` en noir, et il faudrait
    deux fichiers a garder d'accord."""
    html = render_to_string("partials/logo.html")

    assert "currentColor" in html
    assert "var(--brand)" in html


def test_the_pages_declare_a_favicon(client, recruteur):
    client.force_login(recruteur)

    contenu = client.get("/").content.decode()

    assert 'rel="icon"' in contenu
    assert "mark.svg" in contenu
    assert 'name="theme-color"' in contenu


def test_the_sidebar_shows_the_mark_next_to_the_name(client, recruteur):
    client.force_login(recruteur)

    contenu = client.get("/").content.decode()

    assert "sidebar__brand" in contenu
    assert "<svg class=\"logo\"" in contenu
    assert "sidebar__brand-nom" in contenu


def test_the_standalone_mark_survives_a_dark_tab():
    """Une favicon posee sur un onglet sombre en encre sombre disparait."""
    svg = brand.marque_svg()

    assert "prefers-color-scheme: dark" in svg
