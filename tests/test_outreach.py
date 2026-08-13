"""Echanges avec les candidats.

Ces tests portent moins sur ce que le module envoie que sur ce qu'il **refuse
d'envoyer**. Un systeme qui ecrit a des candidats est un systeme qui peut leur
nuire : ecrire sur un canal qu'ils n'ont pas autorise, leur promettre ce que le
gabarit ne promet pas, ou — le plus courant — ne rien leur ecrire du tout.

Le dernier point est mesure a part, dans le bloc « le silence ». C'est la
plainte la plus repandue sur le recrutement, et la seule que ce module
pretende chiffrer.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from apps.candidates.models import Application, Candidate
from apps.core.models import AuditLog
from apps.jobs.models import JobOffer, JobSkill
from apps.outreach import (
    backends,
    drafting,
    registry,
    salutation,
    services,
    silence,
)
from apps.outreach.exceptions import (
    CanalNonConnecte,
    ConsentementManquant,
    CoordonneeManquante,
    EnvoiBloque,
    MessageDejaParti,
)
from apps.outreach.models import Channel, Consent, Message


@pytest.fixture(autouse=True)
def sans_modele(monkeypatch):
    """La personnalisation demande un serveur d'inference.

    Elle est neutralisee par defaut : ces tests portent sur les regles, pas sur
    la redaction. Des tests dedies verifient qu'un modele absent laisse un
    brouillon utilisable.
    """
    monkeypatch.setattr(
        drafting, "personnaliser", lambda *args, **kwargs: {}
    )


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter",
        first_name="Sara", last_name="Benali",
    )


@pytest.fixture
def offre(db):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    return offre


@pytest.fixture
def candidature(db, offre):
    candidat = Candidate.objects.create(
        full_name="Youssef Alaoui",
        email="youssef@example.com",
        phone="0600000000",
        total_experience_years=5,
    )
    return Application.objects.create(candidate=candidat, offer=offre)


# --- Par quel prenom appeler quelqu'un ---------------------------------------
@pytest.mark.parametrize("nom,attendu", [
    # Capitales sur le nom de famille : convention administrative repandue.
    ("EL AMRANI Sara", "Sara"),
    ("BENALI Ahmed", "Ahmed"),
    ("Sara EL AMRANI", "Sara"),
    # Ordre occidental, casse uniforme.
    ("Sara El Amrani", "Sara"),
    ("Youssef Alaoui", "Youssef"),
    ("Karim Benjelloun", "Karim"),
    ("Jean-Pierre Dupont", "Jean-Pierre"),
    # Un seul mot : rien a choisir.
    ("Youssef", "Youssef"),
    # Ecritures sans casse : la convention des capitales n'y existe pas.
    ("سارة العمراني", "سارة"),
    # On renonce : aucun signal d'ordre, ou un nom de famille en tete.
    ("BADR SAHRAOUI", ""),
    ("ALAOUI YOUSSEF", ""),
    ("El Amrani", ""),
    ("Ould Cheikh", ""),
    ("", ""),
    ("   ", ""),
])
def test_the_first_name_is_found_or_given_up_on(nom, attendu):
    """Se tromper de prenom dans un courrier de recrutement est pire que de ne
    pas en mettre. Le premier essai donnait « Bonjour EL, » a Sara EL AMRANI."""
    assert salutation.prenom(nom) == attendu


def test_the_greeting_never_uses_a_family_name_particle():
    assert salutation.formule("EL AMRANI Sara") == "Bonjour Sara,"
    assert salutation.formule("El Amrani") == "Bonjour,"
    assert salutation.formule("BADR SAHRAOUI") == "Bonjour,"
    assert salutation.formule("Sara El Amrani", blind=True) == "Bonjour,"


def test_the_demo_names_all_get_an_acceptable_greeting(db, offre):
    """Aucun nom du jeu de demonstration ne doit produire une formule fausse."""
    NOMS = [
        "Ahmed Benali", "BADR SAHRAOUI", "EL AMRANI Sara", "Karim Benjelloun",
        "Leila Fassi", "Sara El Amrani", "Youssef Alaoui",
    ]
    FAUX = {"Bonjour EL,", "Bonjour El,", "Bonjour Ben,", "Bonjour Ould,"}

    formules = {nom: salutation.formule(nom) for nom in NOMS}

    assert not (set(formules.values()) & FAUX), formules
    assert formules["EL AMRANI Sara"] == "Bonjour Sara,"
    assert formules["Sara El Amrani"] == "Bonjour Sara,"


# --- Consentement ------------------------------------------------------------
def test_email_and_call_are_presumed_open(candidature):
    """Le candidat a donne ces coordonnees pour cet usage et attend une reponse."""
    candidat = candidature.candidate

    assert services.autorise(candidat, Channel.EMAIL)
    assert services.autorise(candidat, Channel.CALL)


def test_whatsapp_and_sms_require_an_explicit_agreement(candidature):
    candidat = candidature.candidate

    assert not services.autorise(candidat, Channel.WHATSAPP)
    assert not services.autorise(candidat, Channel.SMS)


def test_an_agreement_opens_a_channel_that_was_closed(candidature, recruteur):
    candidat = candidature.candidate

    services.enregistrer_consentement(
        candidat, channel=Channel.WHATSAPP, granted=True, actor=recruteur
    )

    assert services.autorise(candidat, Channel.WHATSAPP)


def test_a_withdrawal_closes_a_channel_that_was_presumed_open(candidature, recruteur):
    """Le cas qui compte le plus : quelqu'un qui demande a ne plus etre appele
    doit etre entendu, meme si l'appel etait justifie par sa candidature."""
    candidat = candidature.candidate

    services.enregistrer_consentement(
        candidat, channel=Channel.CALL, granted=False, actor=recruteur,
        source=Consent.Source.WITHDRAWN,
    )

    assert not services.autorise(candidat, Channel.CALL)


def test_a_consent_never_overwrites_the_previous_one(candidature, recruteur):
    """Prouver qu'un accord existait au moment de l'envoi suppose l'historique."""
    candidat = candidature.candidate

    services.enregistrer_consentement(
        candidat, channel=Channel.WHATSAPP, granted=True, actor=recruteur
    )
    services.enregistrer_consentement(
        candidat, channel=Channel.WHATSAPP, granted=False, actor=recruteur
    )

    assert Consent.objects.filter(candidate=candidat).count() == 2
    assert not services.autorise(candidat, Channel.WHATSAPP)


def test_when_two_records_share_a_timestamp_the_refusal_wins(candidature, recruteur):
    """L'horloge de Windows avance par paliers d'environ 15 ms, et la cle
    primaire est un UUID : deux enregistrements poses dans le meme tic ne se
    departagent pas. Trier sur la seule date rendait le resultat aleatoire, et
    un retrait pouvait ne pas prendre effet.

    Quand on ne sait pas lequel est arrive en second, on n'ecrit pas.
    """
    candidat = candidature.candidate
    instant = timezone.now()
    for accorde in (True, False):
        consentement = services.enregistrer_consentement(
            candidat, channel=Channel.EMAIL, granted=accorde, actor=recruteur
        )
        Consent.objects.filter(pk=consentement.pk).update(created_at=instant)

    assert Consent.objects.filter(candidate=candidat).count() == 2
    assert not services.autorise(candidat, Channel.EMAIL)

    # Et dans l'autre ordre d'insertion : le resultat ne doit pas en dependre.
    Consent.objects.filter(candidate=candidat).delete()
    for accorde in (False, True):
        consentement = services.enregistrer_consentement(
            candidat, channel=Channel.EMAIL, granted=accorde, actor=recruteur
        )
        Consent.objects.filter(pk=consentement.pk).update(created_at=instant)

    assert not services.autorise(candidat, Channel.EMAIL)


def test_a_later_agreement_still_reopens_a_channel(candidature, recruteur):
    """La preference au refus ne vaut qu'en cas d'egalite : un accord
    posterieur doit continuer de rouvrir le canal, sinon un retrait serait
    definitif."""
    candidat = candidature.candidate
    refus = services.enregistrer_consentement(
        candidat, channel=Channel.EMAIL, granted=False, actor=recruteur
    )
    Consent.objects.filter(pk=refus.pk).update(
        created_at=timezone.now() - dt.timedelta(days=1)
    )
    services.enregistrer_consentement(
        candidat, channel=Channel.EMAIL, granted=True, actor=recruteur
    )

    assert services.autorise(candidat, Channel.EMAIL)


def test_recording_a_consent_is_audited(candidature, recruteur):
    services.enregistrer_consentement(
        candidat := candidature.candidate, channel=Channel.SMS,
        granted=True, actor=recruteur,
    )

    entree = AuditLog.objects.filter(action=AuditLog.Action.CONSENT_RECORDED).first()
    assert entree is not None
    assert entree.metadata["channel"] == Channel.SMS
    assert entree.metadata["granted"] is True
    assert str(candidat.pk) == entree.object_id


# --- Redaction ---------------------------------------------------------------
def test_a_draft_is_produced_without_any_model(candidature, recruteur):
    """Le gabarit deterministe suffit : un serveur injoignable ne laisse pas le
    recruteur devant une page vide."""
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )

    assert message.status == Message.Status.DRAFT
    assert not message.redige_par_un_modele
    assert "Youssef" in message.body
    assert message.template_version == "1.0.0"


def test_a_draft_can_be_prepared_on_a_channel_the_candidate_has_not_allowed(
    candidature, recruteur
):
    """Bloquer la redaction cacherait le probleme au lieu de le poser."""
    message = services.rediger(
        candidature, modele_id="accuse_reception",
        channel=Channel.WHATSAPP, actor=recruteur,
    )

    assert message.status == Message.Status.DRAFT
    with pytest.raises(ConsentementManquant):
        services.envoyer(message, actor=recruteur)


def test_short_channels_get_the_short_body(candidature, recruteur):
    """Coller cinq paragraphes d'e-mail dans un WhatsApp produit un message que
    personne ne lit."""
    long = services.rediger(
        candidature, modele_id="accuse_reception",
        channel=Channel.EMAIL, actor=recruteur,
    )
    court = services.rediger(
        candidature, modele_id="accuse_reception",
        channel=Channel.SMS, actor=recruteur,
    )

    assert len(court.body) < len(long.body) / 2
    assert court.subject == ""


def test_a_rejection_is_not_offered_on_whatsapp_or_sms(candidature, recruteur):
    """Le seul message du lot qui merite d'etre lu au calme."""
    with pytest.raises(ValueError):
        services.rediger(
            candidature, modele_id="refus",
            channel=Channel.WHATSAPP, actor=recruteur,
        )


def test_the_rejection_never_quotes_a_score(candidature, recruteur):
    """Un texte qui « explique » un rejet en reformulant des chiffres se trompe
    tot ou tard, et cette version-la sera la seule que le candidat aura lue."""
    message = services.rediger(
        candidature, modele_id="refus", actor=recruteur,
        motif="le poste demande une experience Django que le CV ne montre pas.",
    )

    # Espaces normalises : le gabarit passe a la ligne au milieu des phrases,
    # et une assertion posee sur le texte brut echouerait sur un message
    # parfaitement correct.
    texte = " ".join(message.body.split())
    assert "%" not in texte
    assert "score" not in texte.lower()
    # Les mentions de procedure, elles, sont obligatoires.
    assert "contester cette decision" in texte
    assert "il classe et prepare, il ne decide pas" in texte
    assert "conservons votre dossier" in texte


def test_blind_screening_keeps_the_name_out_of_the_draft(
    candidature, recruteur, django_user_model
):
    """Afficher le prenom au moment d'ecrire aurait rouvert ce que
    l'attenuation du biais ferme au moment d'evaluer."""
    aveugle = django_user_model.objects.create_user(
        username="aveugle", password="mot-de-passe-de-test-123",
        role="recruiter", blind_screening=True,
    )

    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=aveugle
    )

    assert "Youssef" not in message.body
    assert message.body.startswith("Bonjour,")
    assert message.metadata["blind"] is True


def test_a_draft_records_who_prepared_it(candidature, recruteur):
    message = services.rediger(
        candidature, modele_id="relance", actor=recruteur
    )

    assert message.drafted_by == recruteur
    assert "Sara Benali" in message.body


def test_every_template_renders_with_the_default_values(candidature, recruteur):
    """Un gabarit dont une variable manque leve `KeyError` au moment le plus
    genant : quand un recruteur veut ecrire."""
    for modele in registry.disponibles():
        canal = Channel.EMAIL if Channel.EMAIL in modele.canaux else next(iter(modele.canaux))
        message = services.rediger(
            candidature, modele_id=modele.id, channel=canal, actor=recruteur
        )
        assert message.body.strip()
        assert "{" not in message.body


# --- Envoi -------------------------------------------------------------------
def test_every_subject_stays_ascii(candidature, recruteur):
    """Un objet non-ASCII un peu long est replie par la RFC 2047, et certains
    clients affichent alors le titre precede d'une espace.

    Mesure : un objet ASCII de 84 caracteres ne se replie pas, un objet
    non-ASCII de 61 caracteres se replie. Un tiret cadratin coutait donc une
    espace parasite dans la boite de reception."""
    for modele in registry.disponibles(Channel.EMAIL):
        message = services.rediger(
            candidature, modele_id=modele.id, actor=recruteur,
        )
        assert message.subject.isascii(), f"{modele.id} : {message.subject!r}"


def test_a_long_ascii_subject_survives_a_round_trip(candidature, recruteur, settings):
    """Le controle qui compte : ce que le destinataire lit reellement."""
    import email
    import email.policy

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    candidature.offer.title = "Ingenieur Systemes et Reseaux Confirme, equipe Plateforme"
    candidature.offer.save(update_fields=["title"])
    mail.outbox.clear()

    message = services.rediger(
        candidature, modele_id="proposition", actor=recruteur
    )
    services.envoyer(message, actor=recruteur)

    relu = email.message_from_bytes(
        mail.outbox[0].message().as_bytes(), policy=email.policy.default
    )["Subject"]
    assert relu == message.subject
    assert relu == relu.lstrip(), "objet precede d'une espace"


def test_an_email_really_leaves(candidature, recruteur, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox.clear()
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )

    services.envoyer(message, actor=recruteur)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["youssef@example.com"]
    message.refresh_from_db()
    assert message.status == Message.Status.SENT
    assert message.sent_by == recruteur


def test_whatsapp_says_it_is_not_connected_instead_of_pretending(
    candidature, recruteur
):
    """Un faux expediteur qui journalise « envoye » aurait donne une
    demonstration plus flatteuse et un systeme qui ment."""
    services.enregistrer_consentement(
        candidature.candidate, channel=Channel.WHATSAPP,
        granted=True, actor=recruteur,
    )
    message = services.rediger(
        candidature, modele_id="relance",
        channel=Channel.WHATSAPP, actor=recruteur,
    )

    with pytest.raises(CanalNonConnecte) as exc:
        services.envoyer(message, actor=recruteur)

    assert "WhatsApp Business" in str(exc.value)
    message.refresh_from_db()
    assert message.status == Message.Status.FAILED
    assert message.error


def test_a_missing_address_is_reported_before_anything_leaves(
    offre, recruteur, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox.clear()
    sans_adresse = Application.objects.create(
        candidate=Candidate.objects.create(full_name="Sans Adresse"), offer=offre
    )
    message = services.rediger(
        sans_adresse, modele_id="accuse_reception", actor=recruteur
    )

    with pytest.raises(CoordonneeManquante):
        services.envoyer(message, actor=recruteur)

    assert mail.outbox == []


def test_the_public_demonstration_sends_nothing_at_all(
    candidature, recruteur, settings
):
    """Une demonstration en ligne qui expedie de vrais courriers a des adresses
    saisies par des inconnus est un incident, pas une fonctionnalite."""
    settings.DEMO_MODE = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox.clear()
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )

    with pytest.raises(EnvoiBloque):
        services.envoyer(message, actor=recruteur)

    assert mail.outbox == []


def test_a_sent_message_is_never_sent_twice(candidature, recruteur, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )
    services.envoyer(message, actor=recruteur)

    with pytest.raises(MessageDejaParti):
        services.envoyer(message, actor=recruteur)


def test_a_sent_message_is_no_longer_editable(candidature, recruteur, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )
    assert message.modifiable

    services.envoyer(message, actor=recruteur)

    message.refresh_from_db()
    assert not message.modifiable


def test_sending_is_audited_with_the_template_version(
    candidature, recruteur, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )

    services.envoyer(message, actor=recruteur)

    entree = AuditLog.objects.filter(action=AuditLog.Action.MESSAGE_SENT).first()
    assert entree is not None
    assert entree.metadata["template"] == "accuse_reception"
    assert entree.metadata["template_version"] == "1.0.0"
    assert entree.metadata["redige_par_un_modele"] is False


# --- Echanges consignes ------------------------------------------------------
def test_a_call_is_logged_not_marked_as_sent(candidature, recruteur):
    """Une declaration humaine, pas une trace technique. Les confondre donnerait
    au journal une autorite qu'il n'a pas."""
    message = services.consigner(
        candidature, channel=Channel.CALL,
        body="Appel de 10 minutes, le candidat confirme sa disponibilite.",
        actor=recruteur, direction=Message.Direction.OUTBOUND,
    )

    assert message.status == Message.Status.LOGGED
    assert not message.parti
    assert AuditLog.objects.filter(action=AuditLog.Action.MESSAGE_LOGGED).exists()


# --- Le silence --------------------------------------------------------------
def _ecarter(candidature, recruteur, *, il_y_a_jours: int):
    quand = timezone.now() - dt.timedelta(days=il_y_a_jours)
    Application.objects.filter(pk=candidature.pk).update(
        stage=Application.Stage.REJECTED,
        decided_by=recruteur,
        decided_at=quand,
        decision_note="Profil trop eloigne du poste.",
    )
    candidature.refresh_from_db()
    return candidature


def test_a_rejected_candidate_never_told_is_counted(candidature, recruteur):
    """Le pire des deux silences : l'information existe, elle est ecrite, et
    elle n'est pas transmise."""
    _ecarter(candidature, recruteur, il_y_a_jours=40)

    mesure = silence.mesurer()

    assert mesure.ecartes == 1
    assert mesure.ecartes_sans_reponse == 1
    assert mesure.pourcentage == 100
    assert mesure.plus_ancien.jours == 40
    assert mesure.plus_ancien.apres_decision


def test_telling_the_candidate_clears_the_silence(candidature, recruteur, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    _ecarter(candidature, recruteur, il_y_a_jours=10)
    message = services.rediger(candidature, modele_id="refus", actor=recruteur)
    services.envoyer(message, actor=recruteur)

    mesure = silence.mesurer()

    assert mesure.ecartes_sans_reponse == 0
    assert mesure.ecartes_prevenus == 1
    assert mesure.irreprochable


def test_a_logged_call_counts_as_an_answer(candidature, recruteur):
    """La question est « cette personne a-t-elle eu une reponse », pas « le
    logiciel a-t-il expedie quelque chose »."""
    _ecarter(candidature, recruteur, il_y_a_jours=5)
    services.consigner(
        candidature, channel=Channel.CALL, body="Refus annonce par telephone.",
        actor=recruteur, direction=Message.Direction.OUTBOUND,
    )

    assert silence.mesurer().ecartes_sans_reponse == 0


def test_a_message_sent_before_the_decision_does_not_count(
    candidature, recruteur, settings
):
    """Un accuse de reception ne previent pas d'un rejet decide ensuite."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    accuse = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )
    services.envoyer(accuse, actor=recruteur)
    _ecarter(candidature, recruteur, il_y_a_jours=0)

    mesure = silence.mesurer()

    assert mesure.ecartes_sans_reponse == 1


def test_an_inbound_message_is_not_an_answer_from_us(candidature, recruteur):
    """Un message recu du candidat ne le renseigne sur rien."""
    _ecarter(candidature, recruteur, il_y_a_jours=12)
    services.consigner(
        candidature, channel=Channel.EMAIL, body="Le candidat relance.",
        actor=recruteur, direction=Message.Direction.INBOUND,
    )

    assert silence.mesurer().ecartes_sans_reponse == 1


def test_an_open_file_left_without_a_word_is_the_second_silence(
    candidature, recruteur
):
    """Le candidat ne sait meme pas si sa candidature est arrivee."""
    Application.objects.filter(pk=candidature.pk).update(
        applied_at=timezone.now() - dt.timedelta(days=silence.JOURS_AVANT_SILENCE + 9)
    )

    mesure = silence.mesurer()

    assert mesure.ouverts_anciens == 1
    assert mesure.ouverts_sans_message == 1
    assert not mesure.plus_ancien.apres_decision


def test_a_recent_open_file_is_a_delay_not_a_silence(candidature):
    Application.objects.filter(pk=candidature.pk).update(
        applied_at=timezone.now() - dt.timedelta(days=2)
    )

    mesure = silence.mesurer()

    assert mesure.ouverts_anciens == 0
    assert mesure.irreprochable


def test_the_oldest_neglected_file_comes_first(offre, recruteur):
    """C'est l'anciennete du silence qui coute, pas le nombre."""
    for index, jours in enumerate((5, 60, 20)):
        candidat = Candidate.objects.create(full_name=f"Candidat {index}")
        candidature = Application.objects.create(candidate=candidat, offer=offre)
        _ecarter(candidature, recruteur, il_y_a_jours=jours)

    oublis = silence.mesurer().oublis

    assert [item.jours for item in oublis] == [60, 20, 5]


def test_the_median_notification_delay_is_measured(offre, recruteur, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    for index in range(3):
        candidat = Candidate.objects.create(
            full_name=f"Prevenu {index}", email=f"p{index}@example.com"
        )
        candidature = Application.objects.create(candidate=candidat, offer=offre)
        _ecarter(candidature, recruteur, il_y_a_jours=10)
        message = services.rediger(candidature, modele_id="refus", actor=recruteur)
        services.envoyer(message, actor=recruteur)

    mesure = silence.mesurer()

    assert mesure.delai_median_jours == pytest.approx(10, abs=0.1)


# --- Diagnostic de la configuration ------------------------------------------
def test_the_check_refuses_to_conclude_without_credentials(db, settings):
    """Sans identifiants, Django n'appelle pas `login()` : la connexion s'ouvre
    et l'outil annoncerait « prete » alors que rien n'a ete verifie. Un
    controle qui reussit a vide est pire qu'absent."""
    settings.EMAIL_HOST = "smtp.example.com"
    settings.EMAIL_HOST_USER = ""
    settings.EMAIL_HOST_PASSWORD = ""

    with pytest.raises(SystemExit):
        call_command("check_email")


def test_the_check_says_nothing_when_no_server_is_configured(db, settings, capsys):
    settings.EMAIL_HOST = ""

    call_command("check_email")

    assert "EMAIL_HOST est vide" in capsys.readouterr().out


def _serveur_qui_refuse(settings, monkeypatch, code: int):
    """Configure un vrai backend SMTP dont la connexion echoue sur `code`.

    Le backend en memoire ne convient pas ici : son `open()` reussit toujours,
    et le monkeypatch pose sur le backend SMTP ne serait jamais atteint — le
    test passerait sans rien eprouver.
    """
    import smtplib

    from django.core.mail.backends.smtp import EmailBackend

    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = "smtp-relay.example.com"
    settings.EMAIL_HOST_USER = "compte@example.com"

    def _refuse(self):
        raise smtplib.SMTPAuthenticationError(code, b"refuse")

    monkeypatch.setattr(EmailBackend, "open", _refuse)


def test_the_check_never_prints_the_secret(db, settings, monkeypatch, capsys):
    """Quatre caracteres d'un secret sont quatre caracteres de moins a deviner."""
    _serveur_qui_refuse(settings, monkeypatch, 535)
    settings.EMAIL_HOST_PASSWORD = "un-secret-reconnaissable"

    with pytest.raises(SystemExit):
        call_command("check_email")

    sortie = capsys.readouterr().out
    assert "un-secret-reconnaissable" not in sortie
    assert "renseigne" in sortie


def test_a_blocked_account_is_not_reported_as_a_wrong_key(
    db, settings, monkeypatch, capsys
):
    """525 n'est pas 535. Confondre les deux fait regenerer une cle correcte en
    boucle, sans jamais toucher la vraie cause."""
    _serveur_qui_refuse(settings, monkeypatch, 525)
    settings.EMAIL_HOST_PASSWORD = "une-cle"

    with pytest.raises(SystemExit):
        call_command("check_email")

    sortie = capsys.readouterr().out
    assert "n'a pas le droit d'envoyer" in sortie
    assert "regenerer ne changera rien" in sortie


def test_wrong_credentials_are_reported_as_such(db, settings, monkeypatch, capsys):
    _serveur_qui_refuse(settings, monkeypatch, 535)
    settings.EMAIL_HOST_PASSWORD = "une-cle"

    with pytest.raises(SystemExit):
        call_command("check_email")

    sortie = capsys.readouterr().out
    assert "refuse les identifiants" in sortie
    assert "n'a pas le droit d'envoyer" not in sortie


def test_the_report_command_runs(candidature, recruteur):
    _ecarter(candidature, recruteur, il_y_a_jours=30)
    call_command("outreach_report")


# --- Personnalisation par le modele ------------------------------------------
def test_an_unreachable_model_leaves_the_template_untouched(
    candidature, recruteur, monkeypatch
):
    """Le pire resultat possible est le gabarit generique, jamais un courrier
    faux envoye a une personne reelle."""
    from apps.ai.client import InferenceError

    def _tombe(*args, **kwargs):
        raise InferenceError("serveur injoignable")

    monkeypatch.setattr(drafting, "personnaliser", drafting.personnaliser)
    monkeypatch.setattr("apps.outreach.drafting.chat_client", _tombe)

    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur,
        avec_modele=True,
    )

    assert message.body.strip()
    assert not message.redige_par_un_modele


def test_a_rambling_model_output_is_discarded(candidature, recruteur, monkeypatch):
    """Le modele qui repart en dissertation : le gabarit vaut mieux."""
    base = {"body": "Bonjour, texte de base court mais complet."}

    assert not drafting._plausible("x" * 5000, base["body"])
    assert not drafting._plausible("ok", base["body"])
    assert drafting._plausible(
        "Bonjour, texte de base court mais complet, un peu reformule.",
        base["body"],
    )


def test_the_model_never_receives_the_raw_cv(candidature):
    """Il ne peut pas ecrire au candidat une chose que le systeme ne sait pas."""
    elements = drafting._elements(candidature)

    assert all(len(ligne) < 200 for ligne in elements)
    assert len(elements) <= drafting.MAX_ELEMENTS


def test_blind_mode_hides_identity_from_the_model_too(candidature):
    """Masquer l'identite a l'ecran puis la donner au modele qui redige le
    courrier aurait fait de l'attenuation du biais une formalite."""
    candidature.candidate.headline = "Developpeur chez OCP Group"
    candidature.candidate.save(update_fields=["headline"])

    visible = "\n".join(drafting._elements(candidature, blind=False))
    aveugle = "\n".join(drafting._elements(candidature, blind=True))

    assert "OCP Group" in visible
    assert "OCP Group" not in aveugle


# --- Interface ---------------------------------------------------------------
def test_the_thread_page_renders(client, candidature, recruteur):
    client.force_login(recruteur)

    reponse = client.get(f"/echanges/candidature/{candidature.pk}/")

    assert reponse.status_code == 200
    contenu = reponse.content.decode()
    assert "Canaux et consentement" in contenu
    assert "modelise, non connecte" in contenu or "non" in contenu


def test_the_silence_page_renders(client, candidature, recruteur):
    _ecarter(candidature, recruteur, il_y_a_jours=25)
    client.force_login(recruteur)

    reponse = client.get("/echanges/")

    assert reponse.status_code == 200
    contenu = reponse.content.decode()
    assert "Dossiers a reprendre" in contenu
    assert "Youssef Alaoui" in contenu


def test_a_read_only_account_cannot_send(client, candidature, django_user_model):
    """Ecrire a un candidat modifie son dossier : la regle du projet
    s'applique ici comme ailleurs."""
    lecteur = django_user_model.objects.create_user(
        username="lecteur", password="mot-de-passe-de-test-123", role="viewer"
    )
    client.force_login(lecteur)

    reponse = client.post(
        f"/echanges/candidature/{candidature.pk}/rediger/",
        {"modele": "accuse_reception", "channel": Channel.EMAIL},
    )

    assert reponse.status_code in (302, 403)
    assert not Message.objects.exists()


def test_editing_a_draft_before_sending_drops_the_model_attribution(
    client, candidature, recruteur, settings
):
    """Un texte repris a la main n'est plus la sortie du modele : le journal ne
    doit pas continuer de l'attribuer au prompt."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    message = services.rediger(
        candidature, modele_id="accuse_reception", actor=recruteur
    )
    Message.objects.filter(pk=message.pk).update(
        prompt_id="outreach_message", prompt_version="1.0.0", model_name="qwen"
    )
    client.force_login(recruteur)

    client.post(
        f"/echanges/message/{message.pk}/envoyer/",
        {"body": "Texte entierement reecrit par le recruteur.", "subject": "Objet"},
    )

    message.refresh_from_db()
    assert message.status == Message.Status.SENT
    assert not message.redige_par_un_modele
    assert message.body.startswith("Texte entierement reecrit")


def test_backends_declares_which_channels_are_real():
    assert backends.canal_connecte(Channel.EMAIL)
    assert not backends.canal_connecte(Channel.WHATSAPP)
    assert not backends.canal_connecte(Channel.SMS)


def test_a_phone_call_is_not_a_missing_integration():
    """Le ranger avec WhatsApp laisserait croire qu'il manque du code a ecrire,
    alors qu'il ne manque rien : un appel se passe, puis se consigne."""
    assert backends.etat_du_canal(Channel.EMAIL) == "connecte"
    assert backends.etat_du_canal(Channel.WHATSAPP) == "connectable"
    assert backends.etat_du_canal(Channel.SMS) == "connectable"
    assert backends.etat_du_canal(Channel.CALL) == "hors_logiciel"
