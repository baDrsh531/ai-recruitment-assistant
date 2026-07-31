"""Fabrique des CV en PDF a partir d'un profil connu.

L'interet : la verite terrain n'est pas annotee a la main, elle est **connue
par construction**. On part d'un profil structure, on le met en page, on
demande au systeme de le reconstituer, et on compare a la source.

Quatre mises en page, choisies pour eprouver des voies differentes du
pipeline :

    simple        une colonne, texte natif        -> modele texte
    deux_colonnes encart lateral + corps          -> detection multi-colonnes
    tableau       competences et langues en table -> extraction depuis un tableau
    scanne        page rendue en image, sans texte -> voie vision obligatoire

Limite a garder en tete : ces CV sont plus propres que les vrais. Ils ne
comportent ni abreviation exotique, ni mise en page fantaisiste, ni scan de
travers. Les scores obtenus dessus sont donc optimistes, et le disent.
"""

from __future__ import annotations

import fitz

LAYOUTS = ("simple", "deux_colonnes", "tableau", "scanne", "arabe")

# L'arabe demande une police contenant ses glyphes et un moteur capable de
# faconnage contextuel. `insert_textbox` ne fait ni l'un ni l'autre : il rend
# les lettres detachees et dans le mauvais sens. `insert_htmlbox`, qui s'appuie
# sur le moteur de mise en page de MuPDF, produit un rendu correct — verifie a
# l'oeil sur la sortie, pas seulement par extraction.
POLICES_ARABES = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)

TITLE_SIZE = 15
HEADING_SIZE = 9.5
BODY_SIZE = 9


def build(profile: dict, layout: str = "simple") -> bytes:
    """Rend le profil en PDF selon la mise en page demandee."""
    if layout not in LAYOUTS:
        raise ValueError(f"Mise en page inconnue : {layout}. Attendu : {LAYOUTS}")

    if layout == "deux_colonnes":
        data = _two_columns(profile)
    elif layout == "tableau":
        data = _with_table(profile)
    elif layout == "arabe":
        data = _arabic(profile)
    else:
        data = _single_column(profile)

    if layout == "scanne":
        data = _flatten_to_image(data)
    return data


# --- Blocs de texte ---------------------------------------------------------
def _contact_block(profile: dict) -> str:
    lignes = [profile["email"], profile.get("phone", ""), profile.get("location", "")]
    lignes += [profile.get("linkedin", ""), profile.get("github", "")]
    return "\n".join(ligne for ligne in lignes if ligne)


def _skills_block(profile: dict) -> str:
    return "\n".join(profile.get("skills", []))


def _languages_block(profile: dict) -> str:
    return "\n".join(
        f"{item['language']} - {_level_label(item['level'])}"
        for item in profile.get("languages", [])
    )


def _experience_block(profile: dict) -> str:
    parties = []
    for poste in profile.get("experiences", []):
        fin = poste.get("end") or "present"
        parties.append(
            f"{poste['title']} - {poste['company']}\n"
            f"{_fr_month(poste['start'])} - {_fr_month(fin)}, {poste.get('location', '')}\n"
            f"{poste.get('description', '')}"
        )
    return "\n\n".join(parties)


def _education_block(profile: dict) -> str:
    return "\n\n".join(
        f"{item['degree']} - {item['year']}\n{item['institution']}"
        for item in profile.get("education", [])
    )


def _level_label(level: str) -> str:
    return "langue maternelle" if level == "NAT" else level


def _fr_month(value: str) -> str:
    """'2022-01' -> '01/2022'. Format courant sur les CV francophones."""
    if not value or "-" not in value:
        return value
    annee, mois = value.split("-")[:2]
    return f"{mois}/{annee}"


# --- Mises en page ----------------------------------------------------------
def police_arabe() -> str | None:
    """Premiere police du systeme contenant les glyphes arabes."""
    from pathlib import Path

    for chemin in POLICES_ARABES:
        if Path(chemin).is_file():
            return chemin
    return None


def _arabic(profile: dict) -> bytes:
    """CV en arabe, sens droite-a-gauche.

    Ce n'est pas une traduction du CV francais : les intitules de sections sont
    en arabe, le sens de lecture est inverse, et les noms de technologies
    restent en latin — comme sur un vrai CV marocain, ou les deux ecritures
    cohabitent dans la meme ligne.
    """
    document = fitz.open()
    page = document.new_page()

    sections = [
        ("الاسم", profile["full_name"]),
        ("المسمى الوظيفي", profile.get("headline", "")),
        ("الاتصال", _contact_block(profile).replace("\n", " · ")),
        ("المهارات", "، ".join(profile.get("skills", []))),
        ("اللغات", _languages_block(profile).replace("\n", " · ")),
        ("الخبرة المهنية", _experience_block(profile)),
        ("التعليم", _education_block(profile)),
    ]
    corps = "".join(
        f"<h3>{titre}</h3><p>{contenu.replace(chr(10), '<br>')}</p>"
        for titre, contenu in sections
        if contenu
    )
    html = f'<div dir="rtl" style="text-align: right">{corps}</div>'

    police = police_arabe()
    css = ""
    if police:
        # Chemin en barres obliques : le CSS traite l'antislash comme une
        # echappement et « C:\Windows\Fonts\arial.ttf » devient
        # « C:WindowsFontsarial.ttf ». La police n'est alors pas trouvee, le
        # rendu retombe sur une fonte sans glyphes arabes, et le PDF sort vide
        # de son arabe sans qu'aucune erreur ne remonte.
        chemin = police.replace("\\", "/")
        css = f"* {{font-family: ar;}} @font-face {{font-family: ar; src: url({chemin});}}"

    page.insert_htmlbox(fitz.Rect(45, 45, 550, 800), html, css=css)

    data = document.tobytes()
    document.close()
    return data


def _single_column(profile: dict) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(50, 45, 545, 95),
        f"{profile['full_name']}\n{profile['headline']}",
        fontsize=TITLE_SIZE,
    )
    corps = (
        "CONTACT\n" + _contact_block(profile) + "\n\n"
        "COMPETENCES\n" + ", ".join(profile.get("skills", [])) + "\n\n"
        "LANGUES\n" + _languages_block(profile) + "\n\n"
        "EXPERIENCE PROFESSIONNELLE\n\n" + _experience_block(profile) + "\n\n"
        "FORMATION\n\n" + _education_block(profile)
    )
    page.insert_textbox(fitz.Rect(50, 105, 545, 800), corps, fontsize=BODY_SIZE)
    data = document.tobytes()
    document.close()
    return data


def _two_columns(profile: dict) -> bytes:
    document = fitz.open()
    page = document.new_page()
    gauche = (
        f"{profile['full_name']}\n{profile['headline']}\n\n"
        "CONTACT\n" + _contact_block(profile) + "\n\n"
        "COMPETENCES\n" + _skills_block(profile) + "\n\n"
        "LANGUES\n" + _languages_block(profile)
    )
    droite = (
        "EXPERIENCE PROFESSIONNELLE\n\n" + _experience_block(profile) + "\n\n"
        "FORMATION\n\n" + _education_block(profile)
    )
    # Couloir vide de 80 points entre les deux colonnes.
    page.insert_textbox(fitz.Rect(45, 45, 235, 800), gauche, fontsize=HEADING_SIZE)
    page.insert_textbox(fitz.Rect(320, 45, 560, 800), droite, fontsize=HEADING_SIZE)
    data = document.tobytes()
    document.close()
    return data


def _with_table(profile: dict) -> bytes:
    """Competences et langues en tableau : `document.paragraphs` les rate."""
    document = fitz.open()
    page = document.new_page()
    # L'en-tete compte jusqu'a sept lignes. Un cadre trop court ne deborde pas :
    # PyMuPDF n'ecrit simplement rien, et le CV sort sans identite — le cas
    # `frontend_tableau` remontait une identite entierement fausse a cause de ca.
    entete = f"{profile['full_name']}\n{profile['headline']}\n{_contact_block(profile)}"
    page.insert_textbox(
        fitz.Rect(50, 40, 545, 140), entete, fontsize=BODY_SIZE
    )

    haut = 175
    page.insert_textbox(
        fitz.Rect(50, haut - 18, 545, haut), "COMPETENCES", fontsize=HEADING_SIZE
    )
    competences = profile.get("skills", [])
    for index, nom in enumerate(competences):
        colonne, ligne = index % 3, index // 3
        x = 50 + colonne * 165
        y = haut + ligne * 18
        page.draw_rect(fitz.Rect(x, y, x + 160, y + 16), color=(0.7, 0.7, 0.7))
        page.insert_textbox(
            fitz.Rect(x + 4, y + 2, x + 156, y + 15), nom, fontsize=BODY_SIZE
        )

    suite = haut + ((len(competences) + 2) // 3) * 18 + 25
    reste = (
        "LANGUES\n" + _languages_block(profile) + "\n\n"
        "EXPERIENCE PROFESSIONNELLE\n\n" + _experience_block(profile) + "\n\n"
        "FORMATION\n\n" + _education_block(profile)
    )
    page.insert_textbox(fitz.Rect(50, suite, 545, 800), reste, fontsize=BODY_SIZE)
    data = document.tobytes()
    document.close()
    return data


def _flatten_to_image(pdf_bytes: bytes) -> bytes:
    """Transforme un PDF en image : plus aucune couche texte exploitable.

    C'est la seule facon de reproduire fidelement un CV scanne, et donc de
    verifier que le diagnostic bascule bien vers le modele vision.
    """
    source = fitz.open(stream=pdf_bytes, filetype="pdf")
    cible = fitz.open()
    for page in source:
        pixmap = page.get_pixmap(dpi=150)
        nouvelle = cible.new_page(width=page.rect.width, height=page.rect.height)
        nouvelle.insert_image(nouvelle.rect, pixmap=pixmap)
    data = cible.tobytes()
    source.close()
    cible.close()
    return data
