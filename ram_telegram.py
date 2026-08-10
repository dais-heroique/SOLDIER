"""
ram_telegram.py — Notification en deux temps + boutons inline
═══════════════════════════════════════════════════════════════════════════
Le cœur du comportement attendu :

  ÉTAPE 1 (< 10 s après publication) — message texte seul, envoyé dès que le
  pré-score dépasse le seuil. Objectif : la VITESSE. Sur un prix évident, on
  peut décider d'acheter à ce stade.

  ÉTAPE 2 (2 à 15 s plus tard) — l'analyse Gemini MODIFIE le message existant
  (editMessageText) plutôt que d'en envoyer un second. Pas de fil qui défile,
  pas de double notification : le message se met à jour sous les yeux.
  `notif_mode: second_message` dans le YAML bascule sur un message séparé.

Anti-spam : au plus une NOUVELLE notification par minute (file triée par
score). Les éditions ne comptent jamais dans cette limite — sinon l'étape 2
serait retardée, ce qui viderait tout le dispositif de son sens.

Aucun achat automatisé : Vinted n'expose pas d'API d'achat, l'automatiser fait
bannir le compte. Les boutons ouvrent l'annonce, on valide à la main.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import ram_config
import ram_db

API = "https://api.telegram.org/bot{token}/{methode}"


class TelegramError(Exception):
    pass


# ─────────────────────── TRANSPORT ───────────────────────
def _appel(methode, charge, token=None, timeout=15):
    token = token or ram_config.secret("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN absent (.env)")
    donnees = json.dumps(charge).encode("utf-8")
    req = urllib.request.Request(API.format(token=token, methode=methode), data=donnees,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reponse = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise TelegramError(f"HTTP {e.code} sur {methode} : {detail}")
    except urllib.error.URLError as e:
        raise TelegramError(f"réseau : {e.reason}")
    except TimeoutError as e:
        # urlopen(timeout=…) lève TimeoutError, PAS URLError. Sans ce cas,
        # un unique délai dépassé remontait jusqu'au worker de notification,
        # qui n'attrape que TelegramError : le thread mourait en silence et
        # plus aucune alerte ne partait, alors que le scan continuait à tourner.
        raise TelegramError(f"délai dépassé après {timeout}s sur {methode} : {e}")
    except (ValueError, OSError) as e:
        # Réponse non-JSON (page d'erreur d'un proxy), connexion coupée…
        raise TelegramError(f"réponse illisible sur {methode} : {e}")
    if not isinstance(reponse, dict):
        raise TelegramError(f"{methode} : réponse inattendue")
    if not reponse.get("ok"):
        raise TelegramError(f"{methode} : {reponse.get('description')}")
    return reponse.get("result")


def _echapper(texte):
    """Échappement HTML (parse_mode=HTML). Plus sûr que Markdown : un titre
    d'annonce contient régulièrement des `*`, `_` ou `[` qui cassent le
    parsing Markdown et font échouer l'envoi — au pire moment."""
    return (str(texte or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ─────────────────────── MISE EN FORME ───────────────────────
def _ligne_prix(annonce):
    prix = float(annonce.get("prix_affiche") or 0)
    port = float(annonce.get("frais_port") or 0)
    protection = float(annonce.get("frais_protection") or 0)
    total = float(annonce.get("prix_total") or prix + port + protection)
    bouts = [f"Prix {prix:.0f}€"]
    if port:
        bouts.append(f"{port:.0f}€ port")
    if protection:
        bouts.append(f"{protection:.2f}€")
    if annonce.get("main_propre"):
        bouts.append("main propre")
    return " + ".join(bouts) + f" = <b>{total:.2f}€</b>"


def age_lisible(publie_le, maintenant=None):
    """« il y a 3 min », « il y a 2 h », « il y a 5 j ».

    C'est l'information la plus décisive après le prix : sur Vinted une vraie
    affaire part en quelques minutes. Une annonce alléchante encore en ligne
    depuis une semaine n'en est presque jamais une — il y a une raison.
    """
    if not publie_le:
        return None
    secondes = (maintenant or time.time()) - float(publie_le)
    if secondes < 0:
        return "à l'instant"
    if secondes < 90:
        return "à l'instant"
    if secondes < 3600:
        return f"il y a {int(secondes // 60)} min"
    if secondes < 86400:
        heures = int(secondes // 3600)
        return f"il y a {heures} h"
    jours = int(secondes // 86400)
    return f"il y a {jours} j"


def _ligne_age(annonce):
    age = age_lisible(annonce.get("publie_le"))
    if not age:
        return None
    secondes = time.time() - float(annonce["publie_le"])
    if secondes < 900:
        return f"🔥 Publiée <b>{age}</b>"          # fenêtre de tir
    if secondes < 86400:
        return f"🕐 Publiée {age}"
    if secondes < 7 * 86400:
        return f"🕐 Publiée {age} — déjà vue par d'autres"
    duree = age.replace("il y a ", "")     # « il y a 10 j » → « 10 j »
    return f"🐌 En ligne depuis {duree} — si c'était une affaire, elle serait partie"


def _config_lisible(annonce):
    bouts = []
    if annonce.get("nb_modules") and annonce.get("capacite_module_go"):
        bouts.append(f"{annonce['nb_modules']}×{annonce['capacite_module_go']}")
    if annonce.get("frequence_mhz"):
        bouts.append(str(annonce["frequence_mhz"]))
    if annonce.get("cas_latency"):
        bouts.append(f"CL{annonce['cas_latency']}")
    bouts.append("UDIMM")
    return " · ".join(bouts)


def vision_operationnelle(cfg=None):
    """La couche vision va-t-elle réellement analyser cette annonce ?

    Deux conditions : activée dans le YAML ET clé API présente. Sans les deux,
    aucune analyse n'arrivera jamais — il ne faut donc pas l'annoncer dans le
    message (un « ⏳ Analyse en cours… » qui ne se met jamais à jour est pire
    que pas d'analyse du tout : on attend au lieu d'aller voir les photos).
    """
    cfg = cfg or ram_config.get()
    if not cfg.val("vision.actif", True):
        return False
    fournisseur = str(cfg.val("vision.provider", "gemini")).lower()
    cles = {"gemini": "GEMINI_API_KEY"}
    return bool(ram_config.secret(cles.get(fournisseur, "GEMINI_API_KEY")))


def message_etape1(annonce, pre, cfg=None):
    """⚡ NON VÉRIFIÉ — texte seul, envoyé immédiatement."""
    cfg = cfg or ram_config.get()
    lignes = [
        f"⚡ <b>NON VÉRIFIÉ</b> · pré-score {pre.get('pre_score', 0):.0f}",
        "",
        f"« {_echapper(annonce.get('titre'))} »",
        _ligne_prix(annonce),
    ]
    ligne_age = _ligne_age(annonce)
    if ligne_age:
        lignes.append(ligne_age)
    lignes += ["", f"Estimation: {_config_lisible(annonce)}"]
    revente = pre.get("revente_estimee")
    marge = pre.get("marge_estimee")
    if revente is not None and marge is not None:
        lignes.append(f"Revente estimée ~{revente:.0f}€ → marge ~{marge:.0f}€")
    if annonce.get("tier"):
        lignes.append(f"Tier {annonce['tier']}")
    for d in (pre.get("drapeaux") or [])[:3]:
        lignes.append(f"⚠️ {_echapper(d)}")

    if vision_operationnelle(cfg):
        lignes += ["", "⏳ Analyse image en cours..."]
    else:
        # Mode texte seul : on dit clairement ce qui reste à faire à la main.
        lignes += ["", "👁 <i>Vérifie les photos toi-même : longueur de la "
                       "barrette, position de l'encoche, nombre de puces.</i>"]
    return "\n".join(lignes)


def message_etape2(annonce, fin, vision, ref=None):
    """Message enrichi après Gemini : ✅ CONFIRMÉ / 🟡 PROBABLE / 🔍 À VÉRIFIER
    / ❌ REJETÉ."""
    statut = fin.get("statut_verif", "a_verifier")
    v = vision or {}

    # ── Rejet ──
    if statut == "rejete":
        lignes = ["❌ <b>REJETÉ</b> · Gemini", ""]
        motifs = (fin.get("rejet_motif") or "").split(" · ")
        for m in motifs:
            if m:
                lignes.append(f"⚠️ {_echapper(m)}")
        for d in (fin.get("drapeaux") or [])[:4]:
            if d not in motifs:
                lignes.append(f"⚠️ {_echapper(d)}")
        lignes += ["", f"« {_echapper(annonce.get('titre'))} »", _ligne_prix(annonce)]
        return "\n".join(lignes)

    # ── À vérifier : photo illisible ──
    if statut == "a_verifier":
        lignes = [
            "🔍 <b>À VÉRIFIER</b> · photo insuffisante", "",
            f"« {_echapper(annonce.get('titre'))} »",
            _ligne_prix(annonce), "",
            f"Pré-score {annonce.get('pre_score') or 0:.0f} · "
            f"{_config_lisible(annonce)}",
        ]
        if fin.get("rejet_motif"):
            lignes.append(f"⚠️ {_echapper(fin['rejet_motif'])}")
        lignes += ["", "💬 Message prêt à envoyer au vendeur :",
                   f"<i>{_echapper(ram_config.get().val('telegram.message_demande_photo'))}</i>"]
        return "\n".join(lignes)

    # ── Confirmé / probable ──
    entete = ("✅ <b>CONFIRMÉ</b>" if statut == "confirme" else "🟡 <b>PROBABLE</b>")
    lignes = [f"{entete} · score {fin.get('score_final', 0):.0f}", ""]

    ref = ref or fin.get("ref") or {}
    if ref:
        lignes.append(f"<b>{_echapper(ref.get('marque'))} {_echapper(ref.get('gamme'))}</b>")
        detail = [ref.get("part_number"), _config_lisible(annonce), f"Tier {ref.get('tier')}"]
        lignes.append(" · ".join(_echapper(x) for x in detail if x))
        traits = []
        if ref.get("couleur"):
            traits.append(ref["couleur"])
        if ref.get("low_profile"):
            traits.append("low profile")
        traits.append("RGB" if ref.get("rgb") else "non-RGB")
        if ref.get("die_type"):
            traits.append(ref["die_type"])
        lignes.append(" · ".join(_echapper(t) for t in traits))
        lignes.append("")

    lignes.append(_ligne_prix(annonce))
    ligne_age = _ligne_age(annonce)
    if ligne_age:
        lignes.append(ligne_age)
    revente = fin.get("revente_estimee")
    marge = fin.get("marge_reelle")
    pct = fin.get("marge_reelle_pct")
    if revente is not None and marge is not None:
        lignes.append(f"Revente {revente:.0f}€ → <b>marge nette {marge:.0f}€</b> "
                      f"({pct:+.0f}%)")
    for d in (fin.get("details_revente") or [])[:3]:
        lignes.append(f"  · {_echapper(d)}")

    # Bandeau des contrôles visuels : ce qui permet de décider en 8 secondes.
    controles = []
    if v.get("photo_lisible"):
        controles.append("📷 Sticker lisible ✓")
    if v.get("etat_contacts") == "propre":
        controles.append("Contacts propres ✓")
    elif v.get("etat_contacts"):
        controles.append(f"Contacts {v['etat_contacts']} ⚠️")
    if v.get("nb_puces_par_face"):
        ecc = " (non-ECC) ✓" if v["nb_puces_par_face"] in (8, 16) else " ⚠️"
        controles.append(f"{v['nb_puces_par_face']} puces{ecc}")
    if v.get("code_semaine"):
        controles.append(f"batch {v['code_semaine']}")
    if controles:
        lignes += ["", " · ".join(_echapper(c) for c in controles)]

    vendeur = []
    if annonce.get("vendeur_note"):
        vendeur.append(f"⭐{annonce['vendeur_note']}")
    if annonce.get("vendeur_ventes"):
        vendeur.append(f"({annonce['vendeur_ventes']} ventes)")
    if vendeur:
        lignes.append("Vendeur " + " ".join(vendeur))

    for d in (fin.get("drapeaux") or [])[:3]:
        lignes.append(f"⚠️ {_echapper(d)}")

    if v.get("confiance") is not None:
        lignes.append(f"<i>confiance vision {float(v['confiance']):.0%}</i>")
    return "\n".join(lignes)


def message_appariement(appariement, annonce):
    """⚡ COMPLÉTER KIT — la notification la plus rentable du système."""
    lignes = [
        "⚡ <b>COMPLÉTER KIT</b>", "",
        f"Tu as déjà en stock : <b>{_echapper(appariement.get('stock_pn'))}</b>",
        f"« {_echapper(annonce.get('titre'))} »",
        _ligne_prix(annonce), "",
    ]
    if appariement.get("type_appariement") == "parfait":
        lignes.append("🎯 <b>Kit parfait</b> : même PN et même semaine de production "
                      "→ vendable comme « kit assorti », XMP stable garanti")
    elif appariement.get("type_appariement") == "batch_different":
        lignes.append("✅ Même part number, batch différent → vendable, "
                      "mentionner « testé ensemble à XMP »")
    if appariement.get("prix_cible"):
        lignes.append(f"Prix cible max : <b>{appariement['prix_cible']:.0f}€</b>")
    if appariement.get("prix_kit_revente"):
        lignes.append(f"Kit complet revendable {appariement['prix_kit_revente']:.0f}€ "
                      f"→ marge <b>{appariement.get('marge_kit_estimee', 0):.0f}€</b>")
    if appariement.get("bonus_kit_eur"):
        lignes.append(f"<i>+{appariement['bonus_kit_eur']:.0f}€ vs revente des deux "
                      f"barrettes séparément</i>")
    return "\n".join(lignes)


# ─────────────────────── BOUTONS ───────────────────────
def boutons(annonce, etape="1", statut_verif=None):
    """Boutons inline. `callback_data` est limité à 64 octets par Telegram :
    on n'y met qu'une action et un id."""
    aid = annonce.get("id")
    url = annonce.get("url") or "https://www.vinted.fr"

    if statut_verif == "a_verifier":
        return {"inline_keyboard": [[
            {"text": "💬 Demander photo sticker", "callback_data": f"photo:{aid}"},
            {"text": "🗑 Archiver", "callback_data": f"archive:{aid}"},
        ], [{"text": "🔗 Voir l'annonce", "url": url}]]}

    if statut_verif == "rejete":
        return {"inline_keyboard": [[
            {"text": "💬 Demander photo sticker", "callback_data": f"photo:{aid}"},
            {"text": "🗑 Archiver", "callback_data": f"archive:{aid}"},
        ]]}

    libelle_achat = "🛒 ACHETER" if statut_verif == "confirme" else "🛒 VOIR"
    return {"inline_keyboard": [[
        {"text": libelle_achat, "url": url},
        {"text": "💬 Message", "callback_data": f"msg:{aid}"},
        {"text": "❌ Ignorer", "callback_data": f"ignore:{aid}"},
    ]]}


# ─────────────────────── ENVOI / ÉDITION ───────────────────────
def anti_spam_ok(cfg=None):
    """Peut-on envoyer une NOUVELLE notification maintenant ?

    Seau à jetons plutôt qu'un simple délai fixe. Le délai fixe de 60 s avait un
    défaut majeur en pratique : quand trois bonnes affaires sortent dans la même
    minute — ce qui arrive, les vendeurs publient par vagues le soir — seule la
    première partait, et il fallait attendre une minute par annonce suivante.
    Sur un marché où une affaire tient quelques minutes, c'est perdre les deux
    autres.

    Avec le seau : `rafale_max` notifications peuvent partir d'affilée, puis le
    rythme retombe à une par `anti_spam_s`. Les jetons se rechargent
    continûment, donc après une période calme la rafale est de nouveau pleine.

    Les éditions de message (étape 2) ne passent jamais par ici.
    """
    cfg = cfg or ram_config.get()
    delai = float(cfg.val("telegram.anti_spam_s", 60))
    rafale = max(1, int(cfg.val("telegram.rafale_max", 3)))
    if delai <= 0:
        return True

    envois = ram_db.notifications_recentes(delai * rafale)
    return len(envois) < rafale


def notifier_etape1(annonce, pre, cfg=None, chat_id=None):
    """Envoie la notification instantanée. Retourne l'id de ram_notification,
    ou None si non envoyée (dry-run, anti-spam, Telegram désactivé)."""
    cfg = cfg or ram_config.get()
    if not cfg.val("telegram.actif", True):
        return None
    chat_id = chat_id or ram_config.secret("TELEGRAM_CHAT_ID")
    texte = message_etape1(annonce, pre, cfg)

    if cfg.dry_run:
        print(f"[dry-run] étape 1 → {annonce.get('url')}\n{_sans_html(texte)}\n")
        return ram_db.enregistrer_notification({
            "annonce_id": annonce.get("id"), "type": "annonce",
            "chat_id": str(chat_id or "dry-run"), "message_id": None,
            "mode": cfg.notif_mode, "etat": "non_verifie", "texte": texte})

    if not chat_id:
        raise TelegramError("TELEGRAM_CHAT_ID absent (.env)")

    resultat = _appel("sendMessage", {
        "chat_id": chat_id, "text": texte, "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "reply_markup": boutons(annonce, etape="1"),
    })
    return ram_db.enregistrer_notification({
        "annonce_id": annonce.get("id"), "type": "annonce", "chat_id": str(chat_id),
        "message_id": resultat.get("message_id"), "mode": cfg.notif_mode,
        "etat": "non_verifie", "texte": texte})


def notifier_etape2(annonce, fin, vision, cfg=None, ref=None):
    """Enrichit la notification de l'étape 1.

    mode 'edit' (défaut)   : editMessageText sur le message existant.
    mode 'second_message'  : nouveau message, en réponse au premier.

    Si le message d'origine n'existe plus (supprimé, trop ancien pour être
    édité), on retombe automatiquement sur un envoi neuf : une analyse
    terminée ne doit jamais rester invisible.
    """
    cfg = cfg or ram_config.get()
    if not cfg.val("telegram.actif", True):
        return None

    statut = fin.get("statut_verif")
    if statut == "rejete" and not cfg.val("telegram.notifier_rejets", False):
        # Pas de notification de rejet demandée : on met quand même à jour le
        # message existant s'il y en a un (sinon on laisserait un « analyse en
        # cours… » suspendu pour toujours).
        precedent = ram_db.notification_de_annonce(annonce.get("id"))
        if not precedent:
            return None

    texte = message_etape2(annonce, fin, vision, ref)
    clavier = boutons(annonce, etape="2", statut_verif=statut)
    precedent = ram_db.notification_de_annonce(annonce.get("id"))

    if cfg.dry_run:
        print(f"[dry-run] étape 2 ({statut}) → {annonce.get('url')}\n"
              f"{_sans_html(texte)}\n")
        if precedent:
            ram_db.maj_notification(precedent["id"], {
                "etat": statut, "texte": texte, "edite_le": time.time(),
                "nb_editions": (precedent.get("nb_editions") or 0) + 1})
        return precedent["id"] if precedent else None

    chat_id = (precedent or {}).get("chat_id") or ram_config.secret("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise TelegramError("TELEGRAM_CHAT_ID absent (.env)")

    mode = cfg.notif_mode
    if mode == "edit" and precedent and precedent.get("message_id"):
        try:
            _appel("editMessageText", {
                "chat_id": chat_id, "message_id": precedent["message_id"],
                "text": texte, "parse_mode": "HTML",
                "disable_web_page_preview": False, "reply_markup": clavier,
            })
            ram_db.maj_notification(precedent["id"], {
                "etat": statut, "texte": texte, "edite_le": time.time(),
                "nb_editions": (precedent.get("nb_editions") or 0) + 1, "erreur": None})
            return precedent["id"]
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                return precedent["id"]
            # Message introuvable / trop ancien : on bascule sur un envoi neuf.
            ram_db.maj_notification(precedent["id"], {"erreur": str(e)[:200]})

    charge = {"chat_id": chat_id, "text": texte, "parse_mode": "HTML",
              "disable_web_page_preview": False, "reply_markup": clavier}
    if precedent and precedent.get("message_id"):
        charge["reply_to_message_id"] = precedent["message_id"]
        charge["allow_sending_without_reply"] = True
    resultat = _appel("sendMessage", charge)
    return ram_db.enregistrer_notification({
        "annonce_id": annonce.get("id"), "type": "annonce", "chat_id": str(chat_id),
        "message_id": resultat.get("message_id"), "mode": mode,
        "etat": statut, "texte": texte})


def notifier_quota_epuise(annonce, cfg=None):
    """L'annonce reste ⚡ NON VÉRIFIÉ mais on le DIT : un « analyse en cours… »
    qui ne se met jamais à jour est pire que pas d'analyse du tout."""
    cfg = cfg or ram_config.get()
    precedent = ram_db.notification_de_annonce(annonce.get("id"))
    if not precedent or not precedent.get("message_id") or cfg.dry_run:
        return None
    texte = (precedent.get("texte") or "").replace(
        "⏳ Analyse image en cours...",
        "⚠️ <i>Quota vision épuisé — analyse reprogrammée automatiquement</i>")
    try:
        _appel("editMessageText", {
            "chat_id": precedent["chat_id"], "message_id": precedent["message_id"],
            "text": texte, "parse_mode": "HTML",
            "reply_markup": boutons(annonce, etape="1")})
        ram_db.maj_notification(precedent["id"], {
            "etat": "quota_epuise", "texte": texte, "edite_le": time.time()})
    except TelegramError as e:
        ram_db.maj_notification(precedent["id"], {"erreur": str(e)[:200]})
    return precedent["id"]


def notifier_appariement(appariement, annonce, cfg=None):
    """Notification prioritaire ⚡ COMPLÉTER KIT — jamais soumise à l'anti-spam :
    c'est la plus rentable, elle ne doit pas attendre son tour."""
    cfg = cfg or ram_config.get()
    if not cfg.val("telegram.notifier_appariements", True):
        return None
    texte = message_appariement(appariement, annonce)
    chat_id = ram_config.secret("TELEGRAM_CHAT_ID")

    if cfg.dry_run:
        print(f"[dry-run] appariement → {annonce.get('url')}\n{_sans_html(texte)}\n")
        return None

    clavier = {"inline_keyboard": [[
        {"text": "🛒 ACHETER pour compléter", "url": annonce.get("url")},
        {"text": "❌ Ignorer", "callback_data": f"ignore_kit:{appariement.get('id')}"},
    ]]}
    resultat = _appel("sendMessage", {"chat_id": chat_id, "text": texte,
                                      "parse_mode": "HTML", "reply_markup": clavier})
    return ram_db.enregistrer_notification({
        "annonce_id": annonce.get("id"), "appariement_id": appariement.get("id"),
        "type": "appariement", "chat_id": str(chat_id),
        "message_id": resultat.get("message_id"), "mode": cfg.notif_mode,
        "etat": "non_verifie", "texte": texte})


# ─────────────────────── BOUTONS : TRAITEMENT DES CLICS ───────────────────────
def traiter_callback(callback, cfg=None):
    """Traite un clic sur un bouton inline. Retourne un message de confirmation.

    Chaque décision est journalisée (ram_journal_decision) : c'est cette trace
    qui permettra d'affiner le scoring avec du recul — pourquoi tel score de 78
    a été ignoré, pourquoi tel 66 a été acheté.
    """
    cfg = cfg or ram_config.get()
    data = (callback.get("data") or "").strip()
    action, _, cible = data.partition(":")
    try:
        cible_id = int(cible)
    except (TypeError, ValueError):
        return "action inconnue"

    if action == "ignore":
        annonce = ram_db.get_annonce(cible_id)
        ram_db.maj_annonce(cible_id, {"statut": "ignore"})
        ram_db.journaliser("ignore", annonce, motif="bouton Ignorer")
        return "❌ Annonce ignorée"

    if action == "archive":
        annonce = ram_db.get_annonce(cible_id)
        ram_db.maj_annonce(cible_id, {"statut": "archive"})
        ram_db.journaliser("archive", annonce, motif="bouton Archiver")
        return "🗑 Archivée"

    if action in ("msg", "photo"):
        annonce = ram_db.get_annonce(cible_id)
        ram_db.journaliser("message", annonce, motif=f"bouton {action}")
        modele = cfg.val("telegram.message_demande_photo")
        return f"💬 Message à copier :\n\n{modele}"

    if action == "ignore_kit":
        ram_db.maj_appariement(cible_id, {"statut": "ignore"})
        return "❌ Appariement ignoré"

    return "action inconnue"


def repondre_callback(callback_id, texte, alerte=False):
    try:
        _appel("answerCallbackQuery", {"callback_query_id": callback_id,
                                       "text": texte[:200], "show_alert": alerte})
    except TelegramError as e:
        print(f"[telegram] réponse au callback impossible : {e}")


def boucle_callbacks(intervalle=2.0, cfg=None):
    """Long polling des clics de boutons. Tourne dans son propre thread ;
    `offset` garantit qu'un update n'est jamais traité deux fois."""
    cfg = cfg or ram_config.get()
    if not ram_config.secret("TELEGRAM_BOT_TOKEN"):
        print("[telegram] pas de token : boucle de callbacks non démarrée")
        return
    offset = None
    print("[telegram] écoute des boutons inline…")
    while True:
        try:
            charge = {"timeout": 25, "allowed_updates": ["callback_query"]}
            if offset is not None:
                charge["offset"] = offset
            updates = _appel("getUpdates", charge, timeout=35) or []
            for update in updates:
                offset = update["update_id"] + 1
                cb = update.get("callback_query")
                if not cb:
                    continue
                try:
                    reponse = traiter_callback(cb, cfg)
                    repondre_callback(cb["id"], reponse, alerte=reponse.startswith("💬"))
                except Exception as e:
                    print(f"[telegram] erreur de traitement du clic : {e}")
                    repondre_callback(cb["id"], "erreur interne")
        except TelegramError as e:
            # 409 = un autre processus interroge déjà ce bot. L'API Telegram
            # n'autorise qu'un seul getUpdates par token : insister ne ferait
            # que voler les mises à jour à l'autre instance, en alternance, et
            # les boutons répondraient une fois sur deux. On se retire.
            if "409" in str(e) or "terminated by other getUpdates" in str(e):
                print("\n[telegram] ⚠️  Un autre RAM SNIPER écoute déjà ce bot "
                      "(HTTP 409).")
                print("[telegram]     Les boutons des notifications sont gérés par "
                      "l'autre instance.")
                print("[telegram]     Cette instance continue de scanner et de "
                      "notifier normalement.\n")
                return
            print(f"[telegram] getUpdates : {e}")
            time.sleep(10)
        except Exception as e:
            print(f"[telegram] boucle : {e}")
            time.sleep(10)
        time.sleep(intervalle)


_TAGS = re.compile(r"<[^>]+>")


def _sans_html(texte):
    return _TAGS.sub("", texte)


def tester_connexion():
    try:
        moi = _appel("getMe", {})
        return True, f"@{moi.get('username')} ({moi.get('first_name')})"
    except TelegramError as e:
        return False, str(e)


if __name__ == "__main__":
    # Aperçu des messages sans rien envoyer.
    annonce = {
        "id": 1, "titre": "Ram ddr4 32go corsair", "url": "https://www.vinted.fr/items/1",
        "prix_affiche": 45.0, "frais_port": 4.0, "frais_protection": 2.95,
        "prix_total": 51.95, "nb_modules": 2, "capacite_module_go": 16,
        "frequence_mhz": 3200, "cas_latency": 16, "tier": "A",
        "vendeur_note": 4.9, "vendeur_ventes": 127, "pre_score": 72,
    }
    pre = {"pre_score": 72, "revente_estimee": 110, "marge_estimee": 58, "drapeaux": []}
    print("═" * 60, "\nÉTAPE 1\n", "═" * 60)
    print(_sans_html(message_etape1(annonce, pre)))

    ref = {"marque": "Corsair", "gamme": "Vengeance LPX",
           "part_number": "CMK32GX4M2E3200C16", "tier": "A", "couleur": "noir",
           "low_profile": 1, "rgb": 0, "die_type": None}
    vision = {"photo_lisible": True, "etat_contacts": "propre", "nb_puces_par_face": 8,
              "confiance": 0.88, "code_semaine": None}
    fin = {"statut_verif": "confirme", "score_final": 87, "revente_estimee": 115,
           "marge_reelle": 63, "marge_reelle_pct": 121, "details_revente": [], "drapeaux": []}
    print("\n" + "═" * 60, "\nÉTAPE 2 — CONFIRMÉ\n", "═" * 60)
    print(_sans_html(message_etape2(annonce, fin, vision, ref)))

    fin_rejet = {"statut_verif": "rejete",
                 "rejet_motif": "encoche en position DDR3 : ce n'est pas de la DDR4 · "
                                "sticker suspect : relabellisation probable",
                 "drapeaux": []}
    print("\n" + "═" * 60, "\nÉTAPE 2 — REJETÉ\n", "═" * 60)
    print(_sans_html(message_etape2(annonce, fin_rejet, {})))

    fin_verif = {"statut_verif": "a_verifier",
                 "rejet_motif": "photo illisible : sticker non déchiffrable", "drapeaux": []}
    print("\n" + "═" * 60, "\nÉTAPE 2 — À VÉRIFIER\n", "═" * 60)
    print(_sans_html(message_etape2(annonce, fin_verif, {})))

    appar = {"id": 1, "stock_pn": "CMK32GX4M1E3200C16", "type_appariement": "parfait",
             "prix_cible": 48, "prix_kit_revente": 120, "marge_kit_estimee": 42,
             "bonus_kit_eur": 24}
    print("\n" + "═" * 60, "\nAPPARIEMENT\n", "═" * 60)
    print(_sans_html(message_appariement(appar, annonce)))
