"""Modeles de messages versionnes.

Meme regle que pour les prompts : on ne modifie jamais un modele en place, on
incremente sa version. Chaque message envoye conserve l'identifiant et la
version appliques, si bien qu'on peut dire six mois plus tard exactement quel
texte une personne a recu — y compris apres trois refontes de la formulation.

**Ces modeles fonctionnent sans modele de langage.** C'est le meme parti que
pour le score : le moteur deterministe produit un resultat exploitable seul, et
le modele de langage l'ameliore quand il est joignable. Un recruteur devant un
serveur d'inference injoignable obtient ici un brouillon correct, pas une page
vide.

Deux longueurs par modele, et ce n'est pas cosmetique : coller cinq paragraphes
d'e-mail dans un WhatsApp produit un message que personne ne lit. Les canaux
courts recoivent une version courte, ecrite pour eux.

**Les objets restent en ASCII.** Ce n'est pas une coquetterie. Un objet qui
contient un seul caractere hors ASCII est encode selon la RFC 2047, et s'il est
un peu long il est alors replie sur deux lignes : `Subject:` reste vide, et
certains clients affichent le titre precede d'une espace. Mesure sur ce projet :
un objet ASCII de 84 caracteres ne se replie pas, un objet non-ASCII de 61
caracteres se replie. Un tiret cadratin dans un objet coutait donc une espace
parasite dans la boite de reception ; deux-points ne coutent rien.

La limite subsiste pour un intitule de poste accentue dans un objet long, que
ce module ne choisit pas — c'est signale dans les limites du README.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Channel


@dataclass(frozen=True)
class Modele:
    id: str
    version: str
    libelle: str
    objet: str
    corps: str
    # Version destinee aux canaux courts. Vide = ce modele ne s'y prete pas.
    corps_court: str = ""
    canaux: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {Channel.EMAIL, Channel.WHATSAPP, Channel.SMS}
        )
    )
    # Le modele annonce-t-il une sortie du processus ? Sert a exiger un motif
    # et a compter les candidats prevenus.
    annonce_un_refus: bool = False

    def rendre(self, *, court: bool = False, **valeurs: object) -> dict:
        corps = self.corps_court if (court and self.corps_court) else self.corps
        return {
            "subject": "" if court else self.objet.format(**valeurs),
            "body": corps.format(**valeurs).strip(),
            "template_id": self.id,
            "template_version": self.version,
        }

    def accepte(self, channel: str) -> bool:
        return channel in self.canaux


REGISTRE: dict[str, Modele] = {}


def enregistrer(modele: Modele) -> Modele:
    REGISTRE[modele.id] = modele
    return modele


def get(modele_id: str) -> Modele:
    try:
        return REGISTRE[modele_id]
    except KeyError:
        raise KeyError(f"Modele de message inconnu : {modele_id}") from None


def disponibles(channel: str | None = None) -> list[Modele]:
    modeles = list(REGISTRE.values())
    if channel is None:
        return modeles
    return [modele for modele in modeles if modele.accepte(channel)]


# --------------------------------------------------------------------------
ACCUSE_RECEPTION = enregistrer(
    Modele(
        id="accuse_reception",
        version="1.0.0",
        libelle="Accuse de reception",
        objet="Votre candidature au poste de {poste}",
        corps="""
{salutation}

Nous avons bien recu votre candidature au poste de {poste} et nous vous
remercions de l'interet que vous portez a {entreprise}.

Votre dossier est en cours d'examen. Nous revenons vers vous sous {delai}
jours, que notre reponse soit positive ou negative.

Bien cordialement,
{signataire}
{entreprise}
""",
        corps_court=(
            "{salutation} nous avons bien recu votre candidature au poste "
            "de {poste}. Reponse sous {delai} jours. {signataire}, {entreprise}."
        ),
    )
)

INVITATION_ENTRETIEN = enregistrer(
    Modele(
        id="invitation_entretien",
        version="1.0.0",
        libelle="Invitation a un entretien",
        objet="Entretien pour le poste de {poste}",
        corps="""
{salutation}

Votre profil a retenu notre attention pour le poste de {poste}. Nous
souhaiterions echanger avec vous.

Seriez-vous disponible pour un entretien dans les prochains jours ? Indiquez-moi
deux ou trois creneaux qui vous conviennent et je m'organise.

L'echange durera environ {duree} minutes et portera sur votre parcours et sur
les aspects techniques du poste.

Bien cordialement,
{signataire}
{entreprise}
""",
        corps_court=(
            "{salutation} votre profil nous interesse pour le poste de "
            "{poste}. Seriez-vous disponible pour un entretien d'environ "
            "{duree} minutes ? Indiquez-moi deux ou trois creneaux. "
            "{signataire}, {entreprise}."
        ),
    )
)

DEMANDE_INFORMATION = enregistrer(
    Modele(
        id="demande_information",
        # 1.0.1 : le tiret cadratin de l'objet est devenu deux-points. Mesure a
        # l'appui — voir la note sur les objets en tete de fichier.
        version="1.0.1",
        libelle="Demande de precision",
        objet="Votre candidature au poste de {poste} : une precision",
        corps="""
{salutation}

Nous examinons votre candidature au poste de {poste} et il nous manque un
element pour aller plus loin.

{question}

Vous pouvez repondre directement a ce message.

Bien cordialement,
{signataire}
{entreprise}
""",
        corps_court=(
            "{salutation} au sujet de votre candidature au poste de "
            "{poste} : {question} Merci d'avance. {signataire}, {entreprise}."
        ),
    )
)

RELANCE = enregistrer(
    Modele(
        id="relance",
        version="1.0.0",
        libelle="Nouvelles du dossier",
        objet="Ou en est votre candidature au poste de {poste}",
        corps="""
{salutation}

Un mot pour vous dire que votre candidature au poste de {poste} est toujours a
l'etude. Le processus prend plus de temps que prevu et je ne voulais pas vous
laisser sans nouvelles.

Je reviens vers vous des que nous avons avance.

Bien cordialement,
{signataire}
{entreprise}
""",
        corps_court=(
            "{salutation} votre candidature au poste de {poste} est "
            "toujours a l'etude. Le processus prend plus de temps que prevu, je "
            "ne voulais pas vous laisser sans nouvelles. {signataire}."
        ),
    )
)

# Le seul message qui engage l'employeur. D'ou trois precautions absentes des
# autres : il n'annonce aucune condition que le recruteur n'a pas saisie, il
# dit que rien n'est definitif avant un ecrit signe, et il ne part pas par SMS.
# Un « c'est bon pour vous » envoye trop vite se retracte tres mal.
PROPOSITION = enregistrer(
    Modele(
        id="proposition",
        version="1.0.0",
        libelle="Reponse positive",
        objet="Votre candidature au poste de {poste} : suite favorable",
        corps="""
{salutation}

Nous avons le plaisir de vous annoncer que votre candidature au poste de
{poste} est retenue.

{conditions}

Ces elements vous seront confirmes par ecrit ; rien n'est definitif avant la
signature. Si un point demande a etre discute, dites-le-moi simplement en
repondant a ce message.

Nous sommes ravis de vous compter parmi nous.

{signataire}
{entreprise}
""",
        canaux=frozenset({Channel.EMAIL}),
    )
)

# Le refus ne paraphrase pas le score et ne recopie pas les ecarts. Deux
# raisons. Un texte redige qui « explique » un rejet en reformulant des chiffres
# se trompe tot ou tard, et cette version-la sera la seule que le candidat aura
# lue. Et l'explication detaillee existe deja, produite par le moteur, avec ses
# chiffres exacts et sa reserve : la bonne conduite est d'y renvoyer, pas d'en
# ecrire une seconde qui pourrait la contredire.
REFUS = enregistrer(
    Modele(
        id="refus",
        version="1.0.0",
        libelle="Reponse negative",
        objet="Votre candidature au poste de {poste}",
        corps="""
{salutation}

Nous avons examine votre candidature au poste de {poste} avec attention. Nous
ne donnons pas suite : {motif}

Cette decision a ete prise par {signataire}, apres lecture de votre dossier. Un
outil d'aide au tri intervient dans notre processus ; il classe et prepare, il
ne decide pas.

Vous pouvez demander le detail des criteres qui vous ont ete appliques, et
contester cette decision, en repondant a ce message.

Nous conservons votre dossier {retention} jours. Si un poste correspondant a
votre profil s'ouvre d'ici la, nous vous recontacterons.

Nous vous souhaitons une bonne continuation.

{signataire}
{entreprise}
""",
        # Un refus ne s'annonce pas par SMS ni par WhatsApp. Ce n'est pas une
        # limite technique : c'est le seul message du lot qui merite d'etre lu
        # au calme, et le seul qu'on puisse vouloir relire.
        canaux=frozenset({Channel.EMAIL}),
        annonce_un_refus=True,
    )
)
