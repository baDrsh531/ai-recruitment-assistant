"""Rapport d'evaluation exportable en PDF.

Les chiffres du projet — qualite du classement, effet mesure des attributs
identitaires, seuil de coupe et sa marge — vivent aujourd'hui dans des pages
web et une commande. Un responsable conformite, lui, demande un document daté,
versionne et transmissible. C'est ce que produit ce module.

**Aucune dependance ajoutee.** PyMuPDF est deja la, pour lire les CV ; il sait
aussi ecrire des PDF, tables et accents compris. Ajouter ReportLab ou WeasyPrint
pour la seule mise en page aurait alourdi l'installation — WeasyPrint reclame
GTK sous Windows — sans rien apporter que PyMuPDF ne fasse.

**Le document porte sa provenance.** Version du moteur, version de chaque jeu
d'evaluation, date de generation et compte a l'origine de l'export : un rapport
sans ces quatre informations ne prouve rien six mois plus tard. L'export est
lui-meme journalise.

Un piege pour qui verifie le resultat : le texte extrait d'un PDF contient les
**ligatures typographiques** de la police. « Effet » en ressort en « Eﬀet »
(U+FB00), si bien qu'un `"Effet" in texte` echoue sur un document parfaitement
correct. `texte_normalise` defait ces ligatures.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import fitz

from apps.core.models import AuditLog
from apps.core.services import record_audit
from apps.matching.engine import ENGINE_VERSION

# A4 en points PostScript, marges confortables pour une lecture imprimee.
PAGE = fitz.paper_rect("a4")
MARGE = 48
LARGEUR = PAGE.width - 2 * MARGE
PIED = 40

ENCRE = "#0F172A"
ACCENT = "#4F46E5"
DISCRET = "#64748B"
ALERTE = "#B45309"

STYLE = f"""
  body {{ font-family: sans-serif; font-size: 9.5pt; color: {ENCRE};
          line-height: 1.45; }}
  h1 {{ font-size: 20pt; margin: 0 0 2pt 0; }}
  h2 {{ font-size: 12pt; margin: 0 0 6pt 0; color: {ENCRE}; }}
  p  {{ margin: 0 0 6pt 0; }}
  .sous  {{ color: {DISCRET}; font-size: 8.5pt; margin: 0 0 12pt 0; }}
  .note  {{ color: {DISCRET}; font-size: 8pt; margin: 4pt 0 0 0; }}
  .alerte {{ color: {ALERTE}; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 9pt; }}
  th {{ text-align: left; color: {DISCRET}; font-size: 7.5pt;
        text-transform: uppercase; letter-spacing: 0.04em;
        border-bottom: 1px solid #CBD5E1; padding: 3pt 4pt; }}
  td {{ padding: 3pt 4pt; border-bottom: 1px solid #E2E8F0; }}
  td.num, th.num {{ text-align: right; }}
"""


@dataclass
class Sources:
    """Ce que le rapport agrege, et d'ou cela vient."""

    quality: object  # harness.Report
    bias: object  # bias.Report
    mitigations: list
    calibration: object  # threshold.Calibration
    search: object | None = None


class _Redacteur:
    """Empile des blocs HTML en creant des pages au besoin.

    `insert_htmlbox` ne pagine pas : il rend ce qui tient et signale le reste.
    On mesure donc chaque bloc a blanc avant de l'ecrire, et on ouvre une page
    quand il ne tient pas — sinon le contenu deborde en silence, ce qui est la
    pire facon de perdre un chiffre dans un document de conformite.
    """

    def __init__(self, document: fitz.Document) -> None:
        self.document = document
        self.page = None
        self.y = 0.0
        self._nouvelle_page()

    def _nouvelle_page(self) -> None:
        self.page = self.document.new_page(width=PAGE.width, height=PAGE.height)
        self.y = MARGE

    def _hauteur(self, html: str) -> float:
        """Hauteur occupee, mesuree sur une page jetable."""
        brouillon = fitz.open()
        page = brouillon.new_page(width=PAGE.width, height=PAGE.height)
        rect = fitz.Rect(MARGE, MARGE, MARGE + LARGEUR, PAGE.height)
        reste, _ = page.insert_htmlbox(rect, html, css=STYLE)
        brouillon.close()
        # `reste` vaut la hauteur inutilisee du cadre, ou -1 si rien ne tient.
        if reste < 0:
            return PAGE.height
        return (PAGE.height - MARGE) - reste

    def bloc(self, html: str, *, espace: float = 14.0) -> None:
        hauteur = self._hauteur(html)
        if self.y + hauteur > PAGE.height - MARGE - PIED:
            self._nouvelle_page()
        rect = fitz.Rect(MARGE, self.y, MARGE + LARGEUR, PAGE.height - MARGE)
        self.page.insert_htmlbox(rect, html, css=STYLE)
        self.y += hauteur + espace

    def pieds_de_page(self, mention: str) -> None:
        total = self.document.page_count
        for numero, page in enumerate(self.document, start=1):
            page.insert_textbox(
                fitz.Rect(MARGE, PAGE.height - MARGE, MARGE + LARGEUR, PAGE.height - 18),
                f"{mention}    ·    page {numero} / {total}",
                fontsize=7.5,
                color=fitz.utils.getColor("gray"),
                align=fitz.TEXT_ALIGN_CENTER,
            )


# Ligatures que les polices standard produisent, et leur equivalent lisible.
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
             "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}


def texte_normalise(texte: str) -> str:
    """Defait les ligatures d'un texte extrait d'un PDF."""
    for ligature, equivalent in LIGATURES.items():
        texte = texte.replace(ligature, equivalent)
    return texte


def nombre(valeur: float, decimales: int = 3) -> str:
    """Formatte un nombre a la francaise.

    Toute l'interface affiche « 0,997 » ; un rapport qui ecrirait « 0.997 »
    laisserait croire a deux sources differentes pour un meme chiffre.
    """
    return f"{valeur:.{decimales}f}".replace(".", ",")


def _tableau(entetes: list[str], lignes: list[list[str]], numeriques: set[int]) -> str:
    def cellule(balise: str, index: int, valeur: str) -> str:
        classe = ' class="num"' if index in numeriques else ""
        return f"<{balise}{classe}>{valeur}</{balise}>"

    tete = "".join(cellule("th", index, nom) for index, nom in enumerate(entetes))
    corps = "".join(
        "<tr>"
        + "".join(cellule("td", index, valeur) for index, valeur in enumerate(ligne))
        + "</tr>"
        for ligne in lignes
    )
    return f"<table><tr>{tete}</tr>{corps}</table>"


# --- Sections ----------------------------------------------------------------
def _entete(auteur: str, aujourdhui: dt.date) -> str:
    return f"""
      <h1>Rapport d'evaluation</h1>
      <p class="sous">
        Recrutement.IA — moteur {ENGINE_VERSION} · genere le
        {aujourdhui.strftime('%d/%m/%Y')} par {auteur}
      </p>
      <p>
        Ce document rassemble les mesures que le systeme produit sur lui-meme :
        qualite du classement, effet des attributs identitaires sur le score,
        seuil de coupe et sa marge, qualite de la recherche. Toutes sont
        reproductibles par les commandes du depot, sur des jeux annotes dont la
        version figure ci-dessous.
      </p>
      <p class="note">
        Le tri de candidatures releve de l'annexe III.4 de l'AI Act. Ce rapport
        documente les mesures de reduction des biais et la performance du
        systeme ; il ne remplace ni la supervision humaine, ni le journal
        d'audit, qui sont dans l'application.
      </p>
    """


def _section_qualite(rapport) -> str:
    lignes = [
        ["nDCG@5", nombre(rapport.aggregate["ndcg_at_5"])],
        ["Precision@3", nombre(rapport.aggregate["precision_at_3"])],
        ["Exactitude par paires", nombre(rapport.aggregate["pair_accuracy"])],
        ["Correlation de Spearman", nombre(rapport.aggregate["spearman"])],
    ]
    return f"""
      <h2>Qualite du classement</h2>
      <p class="sous">
        Jeu {rapport.dataset} v{rapport.dataset_version} ·
        {len(rapport.cases)} cas annotes a la main
      </p>
      {_tableau(["Metrique", "Valeur"], lignes, {1})}
      <p class="note">
        Le score est calcule par un moteur deterministe : aucun modele de
        langage n'attribue de note, et deux executions sur les memes donnees
        produisent le meme chiffre.
      </p>
    """


def _section_biais(rapport, mitigations, seuil_legal: float) -> str:
    lignes = [
        [
            dimension.dimension,
            nombre(dimension.impact_ratio),
            "oui" if dimension.influences_score else "non",
        ]
        for dimension in rapport.dimensions
    ]
    sous_le_seuil = [d for d in rapport.dimensions if d.impact_ratio < seuil_legal]
    neutralisees = [item for item in mitigations if item.neutralised]

    avertissement = ""
    if sous_le_seuil:
        noms = ", ".join(d.dimension for d in sous_le_seuil)
        avertissement = (
            f'<p class="alerte">Sous le seuil des quatre cinquiemes '
            f"({nombre(seuil_legal, 2)}) : {noms}.</p>"
        )

    return f"""
      <h2>Effet des attributs identitaires</h2>
      <p class="sous">
        Methode des contrefactuels : un seul attribut change a la fois, tout le
        reste du profil est identique. Seuil de reference {nombre(seuil_legal, 2)}
        (regle des quatre cinquiemes, NYC LL144).
      </p>
      {_tableau(["Attribut", "Ratio d'impact", "Influence le score"], lignes, {1})}
      {avertissement}
      <p class="note">
        Attributs neutralises par le screening a l'aveugle :
        {", ".join(item.dimension for item in neutralisees) or "aucun"}.
      </p>
    """


def _section_seuil(calibration) -> str:
    recommande = calibration.recommended
    if recommande is None:
        return "<h2>Seuil de tri</h2><p>Calibration indisponible.</p>"

    from . import threshold as module_seuil

    points = module_seuil.sampled_curve(calibration, step=0.10)
    lignes = [
        [
            f"{point.threshold_percentage} %",
            str(point.retained),
            str(point.false_positive),
            str(point.false_negative),
            nombre(point.precision),
            nombre(point.recall),
        ]
        for point in points
    ]
    reserve = ""
    if calibration.perfectly_separable:
        reserve = (
            f'<p class="alerte">A lire avec mefiance : le seuil separe '
            f"parfaitement le jeu annote, mais sur une marge de "
            f"{calibration.plateau_width_points} point(s) "
            f"({calibration.plateau_low_percentage}–"
            f"{calibration.plateau_high_percentage} %). Cela en dit autant sur "
            f"la facilite du jeu que sur le moteur.</p>"
        )

    return f"""
      <h2>Seuil de tri recommande</h2>
      <p class="sous">
        {calibration.recommended_percentage} % — maximise un F{calibration.beta:.0f},
        ou manquer un bon profil pese quatre fois plus que recevoir un profil
        moyen. Sur {calibration.total_candidates} profils annotes, dont
        {calibration.total_relevant} juges a recevoir.
      </p>
      {_tableau(
          ["Seuil", "Retenus", "A tort", "Manques", "Precision", "Rappel"],
          lignes, {1, 2, 3, 4, 5},
      )}
      {reserve}
      <p class="note">
        Ce seuil n'ecarte aucune candidature : il marque le classement. Toute
        sortie du processus reste la decision d'un recruteur identifie, motivee
        par ecrit et journalisee.
      </p>
    """


def _section_recherche(rapport) -> str:
    if rapport is None:
        return ""
    lignes = [
        ["Rappel@5", nombre(rapport.aggregate.get("recall_at_5", 0))],
        ["  plafond atteignable", nombre(rapport.aggregate.get("recall_at_5_ceiling", 0))],
        ["MRR", nombre(rapport.aggregate.get("mrr", 0))],
        ["Precision@3", nombre(rapport.aggregate.get("precision_at_3", 0))],
    ]
    couche = "BM25 fusionne au vectoriel" if rapport.semantic_used else "BM25 seul"
    return f"""
      <h2>Qualite de la recherche</h2>
      <p class="sous">
        Jeu {rapport.dataset} v{rapport.dataset_version} ·
        {len(rapport.queries)} requetes · couche {couche}
      </p>
      {_tableau(["Metrique", "Valeur"], lignes, {1})}
      <p class="note">
        Le plafond atteignable borne le rappel : une requete comptant plus de
        profils pertinents que de places ne peut pas atteindre 1,000. L'ecart
        entre les deux lignes est le seul manque reel.
      </p>
    """


def _section_provenance(sources: Sources, aujourdhui: dt.date) -> str:
    lignes = [
        ["Moteur de scoring", ENGINE_VERSION],
        ["Jeu de classement", f"{sources.quality.dataset} v{sources.quality.dataset_version}"],
        ["Jeu de seuil", f"{sources.calibration.dataset} v{sources.calibration.dataset_version}"],
    ]
    if sources.search is not None:
        lignes.append(
            ["Jeu de recherche", f"{sources.search.dataset} v{sources.search.dataset_version}"]
        )
    lignes += [
        [
            "Rapprochement semantique",
            "actif" if sources.quality.semantic_used else "inactif (ontologie seule)",
        ],
        ["Date de generation", aujourdhui.strftime("%d/%m/%Y")],
    ]
    return f"""
      <h2>Provenance</h2>
      {_tableau(["Element", "Version"], lignes, set())}
      <p class="note">
        Reproduire ces chiffres : <b>python manage.py evaluate</b>,
        <b>audit_bias --compare-blind</b> et <b>evaluate_search</b>. Un rapport
        sans version de moteur ni version de jeu ne prouve rien : les memes
        commandes sur un autre moteur donneraient d'autres nombres.
      </p>
    """


# --- Point d'entree ----------------------------------------------------------
def build(sources: Sources, *, author: str = "", today: dt.date | None = None) -> bytes:
    """Rend le rapport et renvoie les octets du PDF."""
    aujourdhui = today or dt.date.today()
    auteur = author or "un compte non identifie"

    from .bias import IMPACT_RATIO_THRESHOLD

    document = fitz.open()
    redacteur = _Redacteur(document)
    redacteur.bloc(_entete(auteur, aujourdhui), espace=18)
    redacteur.bloc(_section_qualite(sources.quality))
    redacteur.bloc(_section_biais(sources.bias, sources.mitigations, IMPACT_RATIO_THRESHOLD))
    redacteur.bloc(_section_seuil(sources.calibration))
    recherche = _section_recherche(sources.search)
    if recherche:
        redacteur.bloc(recherche)
    redacteur.bloc(_section_provenance(sources, aujourdhui))

    redacteur.pieds_de_page(
        f"Recrutement.IA · moteur {ENGINE_VERSION} · {aujourdhui.strftime('%d/%m/%Y')}"
    )
    document.set_metadata(
        {
            "title": f"Rapport d'evaluation — moteur {ENGINE_VERSION}",
            "author": auteur,
            "subject": "Qualite du classement, biais mesures, seuil de tri",
            "creator": "Recrutement.IA",
        }
    )
    octets = document.tobytes()
    document.close()
    return octets


def filename(today: dt.date | None = None) -> str:
    jour = (today or dt.date.today()).isoformat()
    return f"recrutement-ia_evaluation_{jour}_moteur-{ENGINE_VERSION}.pdf"


def record_export(actor, *, request=None, size: int, sections: list[str]) -> None:
    """Journalise l'export.

    Un rapport sortant du systeme est une donnee qui circule : savoir qui l'a
    tire et quand fait partie de ce que le journal doit pouvoir montrer.
    """
    record_audit(
        AuditLog.Action.DATA_EXPORTED,
        actor=actor,
        summary=f"Rapport d'evaluation exporte ({size // 1024} Ko)",
        request=request,
        format="pdf",
        engine_version=ENGINE_VERSION,
        sections=sections,
        bytes=size,
    )
