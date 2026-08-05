"""Suggestion de message par le modele de langage.

Le modele **n'ecrit pas** le message : il personnalise un texte deja valide,
produit par le gabarit deterministe. C'est le meme parti que pour l'explication
d'un score — le modele met en forme, il ne decide pas du fond.

La consequence compte plus que le principe : le pire resultat possible est le
gabarit generique, jamais un courrier faux envoye a une personne reelle. Un
serveur injoignable, une reponse tronquee, une sortie hors sujet — dans les
trois cas le recruteur garde un brouillon correct.

Le modele ne voit jamais le CV brut. Il recoit une liste d'elements factuels
courts, tires du profil deja extrait et du score deja calcule. Il ne peut donc
pas ecrire au candidat une chose que le systeme ne sait pas.

**En screening a l'aveugle, rien ne fuit non plus par ici.** Masquer l'identite
a l'ecran puis la donner au modele qui redige le courrier aurait fait de
l'attenuation du biais une formalite.
"""

from __future__ import annotations

import logging

from apps.ai.client import InferenceError, chat_client
from apps.ai.prompts import get as get_prompt
from apps.candidates.models import Application

logger = logging.getLogger(__name__)

# Nombre d'elements factuels transmis. Au-dela, le modele en choisit un au
# hasard pour meubler, ce qui produit un message personnalise sur un detail
# sans importance.
MAX_ELEMENTS = 8

# Une sortie beaucoup plus longue que la base signale un modele qui a brode.
FACTEUR_LONGUEUR_MAX = 2.5


def personnaliser(
    application: Application,
    base: dict,
    *,
    channel: str,
    blind: bool = False,
) -> dict:
    """Renvoie {body, prompt_id, prompt_version, model} ou {} si on garde la base.

    Un dictionnaire vide n'est pas une erreur : c'est la decision de garder le
    gabarit, et l'appelant doit pouvoir continuer sans rien changer.
    """
    elements = _elements(application, blind=blind)
    if not elements:
        # Sans element factuel, « personnaliser » reviendrait a demander au
        # modele d'inventer ce qui rend le message personnel.
        return {}

    prompt = get_prompt("outreach_message")
    messages = prompt.render(
        poste=application.offer.title,
        etape=application.get_stage_display(),
        canal=channel,
        elements="\n".join(f"  - {ligne}" for ligne in elements),
        base=base["body"],
    )

    try:
        reponse = chat_client().chat(
            messages,
            temperature=0.3,
            max_tokens=1200,
            purpose="outreach_message",
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            subject=application,
        )
    except InferenceError as exc:
        logger.info("Personnalisation indisponible pour %s : %s", application.pk, exc)
        return {}

    texte = (reponse.text or "").strip()
    if not _plausible(texte, base["body"]):
        logger.warning(
            "Personnalisation ecartee pour %s : sortie hors des bornes attendues",
            application.pk,
        )
        return {}

    return {
        "body": texte,
        "prompt_id": prompt.id,
        "prompt_version": prompt.version,
        "model": reponse.model,
    }


def _plausible(texte: str, base: str) -> bool:
    """Garde-fou de longueur, applique avant de proposer le texte.

    Ce n'est pas une verification de contenu — aucun test de longueur ne dira
    si un message est juste. Il attrape les deux ratages qu'on observe en
    pratique et qui sont, eux, mecaniques : la reponse tronquee et le modele
    qui repart en dissertation. Dans les deux cas le gabarit vaut mieux.
    """
    if len(texte) < 40:
        return False
    return len(texte) <= len(base) * FACTEUR_LONGUEUR_MAX


def _elements(application: Application, *, blind: bool = False) -> list[str]:
    """Faits courts et verifies que le modele a le droit d'employer.

    Rien ne vient du CV brut : tout vient du profil deja extrait, dont chaque
    element porte sa citation, et du score deja calcule.
    """
    candidat = application.candidate
    offre = application.offer
    lignes: list[str] = []

    if not blind and candidat.headline:
        lignes.append(f"Intitule du profil : {candidat.headline}")
    if candidat.total_experience_years:
        lignes.append(
            f"Experience totale : {candidat.total_experience_years:.0f} ans"
        )

    attendues = {competence.name.lower() for competence in offre.skills.all()}
    communes = [
        competence.name
        for competence in candidat.skills.all()
        if competence.name.lower() in attendues
    ]
    if communes:
        lignes.append(
            "Competences du profil egalement attendues par l'offre : "
            + ", ".join(communes[:6])
        )

    if not blind:
        dernier_poste = candidat.experiences.first()
        if dernier_poste is not None and dernier_poste.title:
            lignes.append(f"Poste le plus recent : {dernier_poste.title}")

    langues = [item.language for item in candidat.languages.all()[:3]]
    if langues:
        lignes.append("Langues declarees : " + ", ".join(langues))

    return lignes[:MAX_ELEMENTS]
