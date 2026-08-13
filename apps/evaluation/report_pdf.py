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


def _couleur(hexa: str) -> tuple[float, float, float]:
    """« #4F46E5 » vers le triplet 0–1 attendu par PyMuPDF."""
    valeur = hexa.lstrip("#")
    return tuple(int(valeur[i:i + 2], 16) / 255 for i in (0, 2, 4))


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

    # Hauteur du bandeau de marque, et cote de la marque elle-meme.
    BANDEAU = 34.0
    MARQUE = 18.0

    def __init__(self, document: fitz.Document) -> None:
        self.document = document
        self.page = None
        self.y = 0.0
        self._nouvelle_page()

    def bandeau_de_marque(self) -> None:
        """Marque et nom en tete de la premiere page, puis un filet.

        Ecrit avec `insert_image` et `insert_text` plutot qu'en HTML : le
        moteur de rendu de `insert_htmlbox` ne charge pas d'image externe, et
        encoder le PNG en base64 dans le HTML gonflerait chaque document pour
        un resultat identique.
        """
        from apps.core import brand

        page = self.page
        haut = self.y
        page.insert_image(
            fitz.Rect(MARGE, haut, MARGE + self.MARQUE, haut + self.MARQUE),
            stream=brand.marque_png(taille=192, encre=brand.ENCRE),
        )
        page.insert_text(
            (MARGE + self.MARQUE + 8, haut + self.MARQUE - 4),
            brand.NOM_RACINE,
            fontsize=11.5,
            fontname="hebo",
            color=fitz.utils.getColor("black"),
        )
        largeur_racine = fitz.get_text_length(
            brand.NOM_RACINE, fontname="hebo", fontsize=11.5
        )
        page.insert_text(
            (MARGE + self.MARQUE + 8 + largeur_racine, haut + self.MARQUE - 4),
            brand.NOM_SUFFIXE,
            fontsize=11.5,
            fontname="hebo",
            color=_couleur(ACCENT),
        )
        filet = haut + self.BANDEAU - 8
        page.draw_line(
            fitz.Point(MARGE, filet),
            fitz.Point(MARGE + LARGEUR, filet),
            color=_couleur("#E2E8F0"),
            width=0.8,
        )
        self.y = haut + self.BANDEAU

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
        from apps.core import brand

        total = self.document.page_count
        marque = brand.marque_png(taille=96, encre="#94A3B8")
        for numero, page in enumerate(self.document, start=1):
            bas = PAGE.height - MARGE
            # La marque en pied de CHAQUE page, pas seulement de la premiere :
            # une page de rapport se photocopie, se transfere et s'imprime
            # seule, et doit dire d'ou elle vient sans le reste du document.
            page.insert_image(
                fitz.Rect(MARGE, bas - 1, MARGE + 9, bas + 8), stream=marque
            )
            page.insert_textbox(
                fitz.Rect(MARGE, bas, MARGE + LARGEUR, PAGE.height - 18),
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
    redacteur.bandeau_de_marque()
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


# --- Dossier d'une candidature -----------------------------------------------
def _dossier_entete(candidature, auteur: str, aujourdhui: dt.date, *, blind: bool) -> str:
    candidat = candidature.candidate
    return f"""
      <h1>{candidat.display_name(blind=blind)}</h1>
      <p class="sous">
        {candidature.offer.title} · {candidature.get_stage_display()} ·
        candidature recue le {candidature.applied_at.strftime('%d/%m/%Y')}
      </p>
      <p class="note">
        Dossier edite le {aujourdhui.strftime('%d/%m/%Y')} par {auteur} —
        moteur {ENGINE_VERSION}.
        {"Screening a l'aveugle : identite masquee." if blind else ""}
      </p>
    """


def _dossier_score(score) -> str:
    if score is None:
        return (
            "<h2>Score de compatibilite</h2>"
            "<p>Cette candidature n'a pas encore ete scoree.</p>"
        )

    lignes = [
        [
            critere["label"],
            nombre(critere["weight"], 2),
            nombre(critere["score"], 2),
        ]
        for critere in score.applicable_criteria
    ]
    correction = ""
    if score.is_overridden:
        correction = (
            f'<p class="alerte">Score corrige manuellement : calcul '
            f"{nombre(score.overall, 2)}, retenu {nombre(score.effective_score, 2)}. "
            f"Motif : {score.override_reason}</p>"
        )
    recevabilite = ""
    facteur = score.breakdown.get("admissibility", 1)
    if facteur < 1:
        recevabilite = (
            f'<p class="note">Facteur de recevabilite {nombre(facteur, 2)} '
            f"applique : les competences obligatoires ne sont que "
            f"partiellement couvertes.</p>"
        )

    return f"""
      <h2>Score de compatibilite — {score.percentage} %</h2>
      <p class="sous">
        Calcule en {score.compute_ms} ms par le moteur {score.engine_version},
        sans appel modele. Deux executions donnent le meme chiffre.
      </p>
      {_tableau(["Critere", "Poids", "Score"], lignes, {1, 2})}
      {correction}
      {recevabilite}
    """


def _dossier_ecarts(score) -> str:
    if score is None or not score.gaps:
        return ""
    lignes = [
        [
            ecart["skill"],
            ecart.get("best_match") or "aucune competence proche",
            nombre(ecart.get("score", 0.0), 2),
        ]
        for ecart in score.gaps
    ]
    return f"""
      <h2>Ecarts sur les competences obligatoires</h2>
      {_tableau(["Attendue", "Le plus proche dans le CV", "Score"], lignes, {2})}
      <p class="note">
        Un ecart n'est pas un motif de rejet : c'est un point a verifier en
        entretien. Une competence absente du profil peut aussi n'avoir pas ete
        extraite du CV.
      </p>
    """


def _dossier_decisions(entrees) -> str:
    if not entrees:
        return (
            "<h2>Decisions</h2>"
            "<p>Aucune decision enregistree. Le moteur classe les candidatures ; "
            "il n'en ecarte aucune.</p>"
        )
    lignes = [
        [
            entree.created_at.strftime("%d/%m/%Y %H:%M"),
            str(entree.actor) if entree.actor else "—",
            entree.metadata.get("stage", ""),
            entree.metadata.get("note", "") or "—",
        ]
        for entree in entrees
    ]
    return f"""
      <h2>Decisions</h2>
      {_tableau(["Date", "Auteur", "Etape", "Motif"], lignes, set())}
      <p class="note">
        Toute sortie du processus est le fait d'un recruteur identifie, motivee
        par ecrit et journalisee. Le journal d'audit est immuable ; ce tableau
        en est un extrait.
      </p>
    """


def _dossier_questions(questions) -> str:
    if not questions:
        return ""
    corps = "".join(
        f"<p><b>{item.theme or 'Question'}</b> — {item.get_intent_display()}<br>"
        f"{item.question}"
        + (
            f'<br><span class="note">ancree sur : « {item.cv_claim[:160]} »</span>'
            if item.cv_claim else ""
        )
        + "</p>"
        for item in questions
    )
    modele = questions[0].model or "modele non renseigne"
    return f"""
      <h2>Questions d'entretien</h2>
      <p class="sous">
        {len(questions)} question(s) generees par {modele}, chacune ancree dans
        une affirmation precise du profil.
      </p>
      {corps}
    """


def build_application(
    application,
    *,
    score=None,
    decisions=(),
    questions=(),
    author: str = "",
    blind: bool = False,
    today: dt.date | None = None,
) -> bytes:
    """Dossier d'une candidature : score detaille, ecarts, decisions, questions.

    Le rapport global dit ce que vaut le systeme ; celui-ci dit ce qui a ete
    fait d'un candidat. C'est le document qu'un candidat peut demander au titre
    de l'article 15 du RGPD, et celui qu'un recruteur emporte en entretien.
    """
    aujourdhui = today or dt.date.today()
    auteur = author or "un compte non identifie"

    document = fitz.open()
    redacteur = _Redacteur(document)
    redacteur.bandeau_de_marque()
    redacteur.bloc(
        _dossier_entete(application, auteur, aujourdhui, blind=blind), espace=18
    )
    redacteur.bloc(_dossier_score(score))
    ecarts = _dossier_ecarts(score)
    if ecarts:
        redacteur.bloc(ecarts)
    redacteur.bloc(_dossier_decisions(list(decisions)))
    entretien = _dossier_questions(list(questions))
    if entretien:
        redacteur.bloc(entretien)

    redacteur.pieds_de_page(
        f"Recrutement.IA · dossier de candidature · {aujourdhui.strftime('%d/%m/%Y')}"
    )
    document.set_metadata(
        {
            "title": f"Dossier — {application.candidate.display_name(blind=blind)}",
            "author": auteur,
            "subject": f"Candidature a « {application.offer.title} »",
            "creator": "Recrutement.IA",
        }
    )
    octets = document.tobytes()
    document.close()
    return octets


# --- Explication destinee au candidat ----------------------------------------
# Le dossier interne est ecrit pour un recruteur : il porte des motifs de
# decision, des comparaisons implicites et un vocabulaire d'outil. Ce
# document-ci s'adresse a la personne concernee, au titre des articles 15 et 22
# du RGPD — droit d'acces, et droit d'obtenir une explication sur une decision
# automatisee. Ce ne sont pas les memes lecteurs, donc pas le meme document.
#
# Ce qu'il contient : les donnees retenues du CV, d'ou elles viennent dans le
# document, comment le score a ete construit, et ce que le candidat peut
# demander. Ce qu'il ne contient pas : les motifs internes de decision, le rang
# du candidat, et toute mention des autres candidatures — ce sont des donnees
# qui concernent d'autres personnes ou des appreciations qui ne lui sont pas
# opposables sous cette forme.

CRITERES_EN_CLAIR = {
    "skills": "Competences attendues par l'offre",
    "experience": "Anciennete demandee",
    "education": "Niveau d'etudes demande",
    "languages": "Langues demandees",
    "certifications": "Certifications demandees",
    "location": "Localisation",
}


def _candidat_entete(candidature, aujourdhui: dt.date) -> str:
    return f"""
      <h1>Votre candidature</h1>
      <p class="sous">
        {candidature.offer.title} — dossier edite le {aujourdhui.strftime('%d/%m/%Y')}
      </p>
      <p>
        Ce document vous explique comment votre candidature a ete analysee. Il
        vous est destine : il vous appartient, et vous pouvez en demander la
        rectification.
      </p>
      <p class="note">
        Aucune decision vous concernant n'est prise automatiquement. Un score de
        compatibilite est calcule par un programme, sans intelligence
        artificielle et sans appreciation ; il aide un recruteur a organiser sa
        lecture. La decision de poursuivre ou non revient a une personne
        identifiee, qui doit la motiver par ecrit.
      </p>
    """


def _candidat_donnees(candidat) -> str:
    lignes = [
        ["Nom", candidat.full_name],
        ["Intitule retenu", candidat.headline or "non renseigne"],
        ["Anciennete retenue", f"{candidat.total_experience_years:.1f} an(s)"],
        ["Competences retenues", str(candidat.skills.count())],
        ["Experiences retenues", str(candidat.experiences.count())],
        ["Formations retenues", str(candidat.education.count())],
    ]
    if candidat.retention_until:
        lignes.append(
            ["Conservation jusqu'au", candidat.retention_until.strftime("%d/%m/%Y")]
        )
    return f"""
      <h2>Ce qui a ete retenu de votre CV</h2>
      {_tableau(["Element", "Valeur"], lignes, set())}
      <p class="note">
        Ces donnees sont extraites de votre CV par un programme. Une erreur
        d'extraction est possible : si l'une d'elles est inexacte, vous pouvez
        en demander la correction.
      </p>
    """


def _candidat_preuves(candidat) -> str:
    """Chaque competence retenue, avec l'extrait du CV qui la justifie.

    La section n'apparait que si au moins une preuve existe. Annoncer « rien
    n'est retenu sans preuve » puis afficher une colonne de tirets serait une
    contradiction, et c'est le candidat qui la lirait.
    """
    avec_preuve = []
    sans_preuve = 0
    for competence in candidat.skills.all()[:25]:
        preuve = competence.evidence
        if preuve is not None and preuve.text:
            extrait = f"« {preuve.text[:90]} »"
            if preuve.page:
                extrait += f" (page {preuve.page})"
            avec_preuve.append([competence.name, extrait])
        else:
            sans_preuve += 1

    if not avec_preuve:
        if not sans_preuve:
            return ""
        return """
          <h2>D'ou vient chaque information</h2>
          <p>
            Votre profil n'a pas ete construit a partir d'un document depose :
            les informations ci-dessus ont ete saisies directement. Il n'y a
            donc pas de passage de CV a vous montrer.
          </p>
        """

    reste = ""
    if sans_preuve:
        reste = (
            f'<p class="note">{sans_preuve} autre(s) competence(s) figurent a '
            f"votre profil sans extrait associe : elles ont ete saisies "
            f"directement plutot que lues dans un document.</p>"
        )

    return f"""
      <h2>D'ou vient chaque information</h2>
      <p class="sous">
        Aucune donnee n'est retenue sans que le passage du document qui la
        justifie puisse etre montre.
      </p>
      {_tableau(["Competence retenue", "Passage de votre CV"], avec_preuve, set())}
      {reste}
    """


def _candidat_score(score) -> str:
    if score is None:
        return (
            "<h2>Analyse de compatibilite</h2>"
            "<p>Votre candidature n'a pas encore ete analysee.</p>"
        )

    lignes = [
        [
            CRITERES_EN_CLAIR.get(critere["name"], critere["label"]),
            f"{nombre(critere['weight'] * 100, 0)} %",
            f"{nombre(critere['score'] * 100, 0)} %",
        ]
        for critere in score.applicable_criteria
    ]
    ecartes = [
        CRITERES_EN_CLAIR.get(critere["name"], critere["label"])
        for critere in score.skipped_criteria
    ]
    mention_ecartes = ""
    if ecartes:
        mention_ecartes = (
            f'<p class="note">Criteres non applicables, l\'offre n\'exprimant '
            f"aucune exigence : {', '.join(ecartes)}. Ils ne vous ont ni "
            f"avantage ni desavantage.</p>"
        )

    return f"""
      <h2>Analyse de compatibilite — {score.percentage} %</h2>
      <p class="sous">
        Chaque critere est note separement, puis pondere. Le poids est fixe par
        l'offre, avant toute candidature.
      </p>
      {_tableau(["Critere", "Poids", "Votre note"], lignes, {1, 2})}
      {mention_ecartes}
      <p class="note">
        Ce pourcentage n'est pas une note sur votre valeur professionnelle :
        c'est une mesure d'ecart entre ce que l'offre demande et ce que votre CV
        indique. Un ecart eleve sur une offre n'en dit rien sur une autre.
      </p>
    """


def _candidat_ecarts(score) -> str:
    if score is None or not score.gaps:
        return ""
    lignes = [
        [
            ecart["skill"],
            ecart.get("best_match") or "aucune competence proche identifiee",
        ]
        for ecart in score.gaps
    ]
    return f"""
      <h2>Ce que l'offre demandait et que le CV n'indiquait pas</h2>
      {_tableau(
          ["Competence demandee", "Ce qui s'en rapprochait le plus"], lignes, set()
      )}
      <p class="note">
        Une competence absente de cette liste n'est pas une competence que vous
        n'avez pas : c'est une competence que votre CV ne mentionnait pas, ou
        que le programme n'a pas su y lire.
      </p>
    """


def _candidat_droits(aujourdhui: dt.date) -> str:
    return f"""
      <h2>Vos droits</h2>
      <p>
        Vous pouvez demander l'acces a vos donnees, leur rectification, leur
        effacement, et une explication sur la maniere dont votre candidature a
        ete traitee. Vous pouvez aussi demander que la decision vous concernant
        soit reexaminee par une personne.
      </p>
      <p class="note">
        Articles 15 et 22 du reglement general sur la protection des donnees.
        Le tri de candidatures est un systeme d'IA a haut risque au sens de
        l'annexe III.4 du reglement europeen sur l'intelligence artificielle :
        chaque etape de votre dossier est enregistree, avec sa date et son
        auteur. Document edite le {aujourdhui.strftime('%d/%m/%Y')}.
      </p>
    """


def build_candidate_explanation(
    application, *, score=None, today: dt.date | None = None
) -> bytes:
    """Explication destinee au candidat lui-meme.

    Deliberement sans le nom du recruteur, sans les motifs de decision, sans
    rang et sans mention des autres candidatures : ce sont des appreciations
    internes ou des donnees concernant d'autres personnes.
    """
    aujourdhui = today or dt.date.today()
    candidat = application.candidate

    document = fitz.open()
    redacteur = _Redacteur(document)
    redacteur.bandeau_de_marque()
    redacteur.bloc(_candidat_entete(application, aujourdhui), espace=18)
    redacteur.bloc(_candidat_donnees(candidat))
    preuves = _candidat_preuves(candidat)
    if preuves:
        redacteur.bloc(preuves)
    redacteur.bloc(_candidat_score(score))
    ecarts = _candidat_ecarts(score)
    if ecarts:
        redacteur.bloc(ecarts)
    redacteur.bloc(_candidat_droits(aujourdhui))

    redacteur.pieds_de_page(
        f"Document destine au candidat · {aujourdhui.strftime('%d/%m/%Y')}"
    )
    document.set_metadata(
        {
            "title": "Votre candidature — explication",
            "subject": f"Candidature a « {application.offer.title} »",
            "creator": "Recrutement.IA",
        }
    )
    octets = document.tobytes()
    document.close()
    return octets


def candidate_explanation_filename(application, today: dt.date | None = None) -> str:
    jour = (today or dt.date.today()).isoformat()
    return f"votre-candidature_{str(application.pk)[:8]}_{jour}.pdf"


def application_filename(application, today: dt.date | None = None) -> str:
    jour = (today or dt.date.today()).isoformat()
    return f"dossier_{str(application.pk)[:8]}_{jour}.pdf"


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
