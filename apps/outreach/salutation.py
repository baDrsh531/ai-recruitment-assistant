"""Trouver par quel prenom appeler quelqu'un — ou renoncer.

Le premier essai de ce module prenait le premier mot du nom complet. Sur le
jeu de demonstration, ca donnait « Bonjour EL, » a Sara EL AMRANI. Le message
serait parti tel quel.

Le probleme n'est pas marocain, il est general : « EL AMRANI Sara » met le nom
de famille devant, « Sara El Amrani » le met derriere, et rien dans la chaine
ne dit lequel des deux on lit. Beaucoup de systemes tranchent quand meme et se
trompent sur une partie de leurs candidats, toujours la meme.

**La regle de ce module est de ne pas deviner.** Trois signaux permettent de
conclure ; hors de ces trois cas il renvoie une chaine vide et le message
commence par « Bonjour, ». Se tromper de prenom dans un courrier de
recrutement est pire que de ne pas en mettre — et le recruteur peut de toute
facon corriger le brouillon avant de l'envoyer.
"""

from __future__ import annotations

# Particules de nom de famille. Un nom qui commence par l'une d'elles ne
# commence pas par un prenom, quelle que soit la casse.
PARTICULES = frozenset(
    {
        "el", "al", "ould", "ben", "bent", "bin", "abd", "abdel", "ait", "ath",
        "de", "del", "della", "di", "da", "das", "dos", "du", "des", "le", "la",
        "van", "von", "der", "den", "ter", "mac", "mc", "o", "saint", "sainte",
    }
)


def _est_en_capitales(mot: str) -> bool:
    """Le mot est-il ecrit tout en capitales ?

    Faux pour un mot sans casse — l'arabe, par exemple : `isupper()` y renvoie
    faux, ce qui est le bon comportement ici puisque la convention des
    capitales n'existe pas dans cette ecriture.
    """
    return mot.isupper() and any(caractere.isalpha() for caractere in mot)


def prenom(nom_complet: str) -> str:
    """Prenom utilisable dans une formule d'appel, ou chaine vide si on ignore.

    Les trois cas ou l'on conclut :

    1. **Casse mixte.** « EL AMRANI Sara » : les capitales marquent le nom de
       famille, convention administrative repandue. Le premier mot qui n'est
       pas en capitales est le prenom.
    2. **Un seul mot.** Il n'y a rien a choisir.
    3. **Casse uniforme et premier mot qui n'est pas une particule.** « Sara El
       Amrani » : ordre occidental, le prenom vient devant.

    Tout le reste renvoie une chaine vide. En particulier un nom entierement en
    capitales sur plusieurs mots — « BADR SAHRAOUI » comme « ALAOUI YOUSSEF » —
    ne porte aucun signal d'ordre : les deux s'ecrivent pareil et se lisent a
    l'envers l'un de l'autre.
    """
    mots = (nom_complet or "").split()
    if not mots:
        return ""
    if len(mots) == 1:
        return "" if mots[0].lower() in PARTICULES else mots[0]

    capitales = [_est_en_capitales(mot) for mot in mots]
    if any(capitales) and not all(capitales):
        for mot, en_capitales in zip(mots, capitales, strict=True):
            if not en_capitales:
                return mot
        return ""

    if all(capitales):
        # Aucun signal d'ordre. On renonce plutot que de tirer a pile ou face
        # sur le nom d'une personne.
        return ""

    return "" if mots[0].lower() in PARTICULES else mots[0]


def formule(nom_complet: str, *, blind: bool = False) -> str:
    """« Bonjour Sara, » ou « Bonjour, » — jamais « Bonjour EL, »."""
    if blind:
        return "Bonjour,"
    trouve = prenom(nom_complet)
    return f"Bonjour {trouve}," if trouve else "Bonjour,"
