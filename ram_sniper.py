"""
ram_sniper.py — Orchestrateur du RAM SNIPER
═══════════════════════════════════════════════════════════════════════════
Trois workers découplés, chacun dans son thread, communiquant par la base :

  1. WORKER SCRAPING   — parcourt les mots-clés Vinted puis Leboncoin, étale
     les requêtes, écrit les annonces, calcule le pré-score. Pousse dans la
     file de notification et dans la file vision.

  2. WORKER NOTIFICATION — sort les annonces par score décroissant et envoie
     au plus une notification par minute (anti-spam). L'étape 1 doit partir
     en moins de 10 s : ce worker ne fait QUE de l'envoi, jamais de réseau
     lent en amont.

  3. WORKER VISION     — dépile ram_vision_file par score décroissant, appelle
     Gemini dans la limite du quota, puis ÉDITE la notification existante.

Le découplage compte : si Gemini est lent ou le quota épuisé, les
notifications instantanées continuent de partir. C'est tout l'intérêt du
flux en deux temps.

Lancement :
    python3 ram_sniper.py                 # tout (scraping + notif + vision)
    python3 ram_sniper.py --dry-run       # aucune notification envoyée
    python3 ram_sniper.py --replay        # rejoue les annonces archivées
    python3 ram_sniper.py --once          # un seul tour de scan puis sortie
    python3 ram_sniper.py --calibrer      # lance le job de calibrage
    python3 ram_sniper.py --etat          # état du système
"""

import heapq
import json
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime

import ram_calibration
import ram_config
import ram_db
import ram_pairing
import ram_parser
import ram_scoring
import ram_scrapers
import ram_telegram
import ram_vision

ARRET = threading.Event()

# File de notification : tas trié par score décroissant. Une annonce à 88 ne
# doit jamais attendre derrière une annonce à 66 arrivée deux secondes plus tôt.
_file_notif = []
_verrou_notif = threading.Lock()

STATS = {"demarre_le": time.time(), "annonces_vues": 0, "annonces_nouvelles": 0,
         "notifiees": 0, "analysees": 0, "rejetees": 0, "appariements": 0,
         "erreurs": 0, "dernier_scan": None, "cycles": 0}


# ─────────────────────── LOGS ───────────────────────
def log(niveau, message, **champs):
    """Logs structurés (JSON) ou lisibles, selon run.log_json."""
    cfg = ram_config.get()
    horodatage = datetime.now().strftime("%H:%M:%S")
    if cfg.val("run.log_json", True):
        entree = {"ts": time.time(), "heure": horodatage, "niveau": niveau,
                  "message": message}
        entree.update(champs)
        print(json.dumps(entree, ensure_ascii=False), flush=True)
    else:
        suffixe = " ".join(f"{k}={v}" for k, v in champs.items())
        print(f"{horodatage} [{niveau}] {message} {suffixe}".rstrip(), flush=True)


# ─────────────────────── WORKER SCRAPING ───────────────────────
def _enfiler_notification(resultat):
    annonce = resultat["annonce"]
    score = resultat["pre"]["pre_score"] if resultat.get("pre") else 0
    with _verrou_notif:
        # heapq est un min-tas : on stocke l'opposé du score pour sortir le
        # meilleur en premier.
        heapq.heappush(_file_notif, (-score, annonce["id"], time.time()))


def traiter_resultats(resultats, cfg):
    """Suite commune à Vinted et Leboncoin : file de notification, file vision,
    et radar d'appariement."""
    for r in resultats:
        if not r or not r.get("nouvelle"):
            continue
        STATS["annonces_nouvelles"] += 1
        annonce = r["annonce"]

        if r["pre"] and r["pre"]["exclusion"]:
            STATS["rejetees"] += 1
            continue

        # ── Radar kits : prioritaire, jamais soumis à l'anti-spam ──
        try:
            appariements = ram_pairing.chercher_appariements(annonce, r["analyse"], cfg)
            for a in appariements:
                STATS["appariements"] += 1
                log("INFO", "appariement détecté", pn=a.get("part_number"),
                    marge_kit=a.get("marge_kit_estimee"), type=a.get("type_appariement"))
                try:
                    ram_telegram.notifier_appariement(a, annonce, cfg)
                    ram_db.maj_appariement(a["id"], {"statut": "notifie",
                                                     "notifie_le": time.time()})
                except ram_telegram.TelegramError as e:
                    log("WARN", "notification d'appariement impossible", erreur=str(e))
        except Exception as e:
            log("WARN", "radar kits", erreur=str(e))

        if r["a_analyser"]:
            ram_db.enfiler_vision(annonce["id"], r["pre"]["pre_score"])
        if r["notifiable"]:
            _enfiler_notification(r)


def worker_scraping(cfg, une_seule_fois=False):
    """Parcourt les mots-clés en boucle, en étalant les requêtes.

    Les mots-clés sont mélangés à chaque cycle : toujours interroger « ddr4 »
    en premier crée un motif reconnaissable côté anti-bot, et fait passer les
    derniers mots-clés systématiquement après les autres.
    """
    scrapers = []
    if cfg.val("sources.vinted.actif", True):
        scrapers.append(ram_scrapers.ScraperVinted(cfg))
    if cfg.val("sources.leboncoin.actif", True):
        scrapers.append(ram_scrapers.ScraperLeboncoin(cfg))

    if not scrapers:
        log("WARN", "aucune source active")
        return

    log("INFO", "worker scraping démarré",
        sources=[s.source for s in scrapers])

    while not ARRET.is_set():
        cfg = ram_config.get()
        STATS["cycles"] += 1
        cycle = {"vues": 0, "retenues": 0, "meilleur": 0.0, "meilleur_titre": None}
        for scraper in scrapers:
            if ARRET.is_set():
                break
            mots = scraper.mots_cles()
            random.shuffle(mots)
            for mot in mots:
                if ARRET.is_set():
                    break
                if not scraper._du_a_passer(mot):
                    continue
                try:
                    resultats = scraper.scanner_mot_cle(mot)
                    STATS["annonces_vues"] += len(resultats)
                    STATS["dernier_scan"] = time.time()
                    traiter_resultats(resultats, cfg)
                    for r in resultats:
                        if not (r and r.get("nouvelle") and r.get("pre")):
                            continue
                        cycle["vues"] += 1
                        if r["pre"].get("exclusion"):
                            continue
                        cycle["retenues"] += 1
                        score = r["pre"].get("pre_score") or 0
                        if score > cycle["meilleur"]:
                            cycle["meilleur"] = score
                            cycle["meilleur_titre"] = (r["annonce"].get("titre") or "")[:60]
                    if resultats:
                        log("INFO", "scan", source=scraper.source, mot_cle=mot,
                            vues=len(resultats),
                            nouvelles=sum(1 for r in resultats if r["nouvelle"]))
                except Exception as e:
                    STATS["erreurs"] += 1
                    log("ERROR", "scan en échec", source=scraper.source,
                        mot_cle=mot, erreur=str(e))
                ram_scrapers._pause(
                    cfg.val(f"sources.{scraper.source}.delai_entre_requetes_s", [2.5, 5.0]))

        # Bilan de fin de cycle. Sans lui, on ne voit défiler que « scan » et
        # rien ne dit si le seuil est presque atteint ou hors d'atteinte : on
        # règle alors les seuils à l'aveugle.
        if cycle["vues"]:
            seuil = float(cfg.val("scoring.seuil_notification", 55))
            log("INFO", "bilan du cycle", nouvelles=cycle["vues"],
                retenues=cycle["retenues"], meilleur_score=round(cycle["meilleur"], 1),
                seuil=seuil,
                verdict=("au moins une notification" if cycle["meilleur"] >= seuil
                         else f"rien n'atteint {seuil:.0f} — voir --diag"),
                meilleure_annonce=cycle["meilleur_titre"])

        if une_seule_fois:
            break
        ARRET.wait(5)


# ─────────────────────── WORKER NOTIFICATION ───────────────────────
def recharger_file_notification(cfg, heures=3):
    """Reconstruit la file d'attente depuis la base.

    La file vit en mémoire : sans cette reconstruction, une annonce était
    définitivement perdue dès qu'un envoi échouait ou que le worker redémarrait.
    La base, elle, sait exactement ce qui reste à faire — toute annonce encore
    en statut « nouveau » au-dessus du seuil n'a jamais été notifiée.

    Bornée à quelques heures : au-delà, l'annonce n'est plus une opportunité,
    la notifier ne ferait que du bruit.
    """
    seuil = float(cfg.val("scoring.seuil_notification", 55))
    with ram_db.get_db() as conn:
        lignes = conn.execute("""
            SELECT id, pre_score FROM ram_annonce
            WHERE statut = 'nouveau' AND exclusion IS NULL
              AND pre_score >= ? AND detecte_le > ? AND encore_en_ligne = 1
              AND id NOT IN (SELECT annonce_id FROM ram_notification
                             WHERE annonce_id IS NOT NULL)
            ORDER BY pre_score DESC LIMIT 200
        """, (seuil, time.time() - heures * 3600)).fetchall()

    ajoutees = 0
    with _verrou_notif:
        deja = {e[1] for e in _file_notif}
        for ligne in lignes:
            if ligne["id"] in deja:
                continue
            heapq.heappush(_file_notif, (-(ligne["pre_score"] or 0), ligne["id"],
                                         time.time()))
            ajoutees += 1
    return ajoutees


def worker_notification(cfg):
    """Envoie les notifications de l'étape 1, la mieux notée d'abord."""
    log("INFO", "worker notification démarré",
        anti_spam_s=cfg.val("telegram.anti_spam_s", 60),
        rafale_max=cfg.val("telegram.rafale_max", 4),
        notif_mode=cfg.notif_mode, dry_run=cfg.dry_run)

    reprises = recharger_file_notification(cfg)
    if reprises:
        log("INFO", "annonces en attente reprises depuis la base", n=reprises)
    derniere_reprise = time.time()

    dernier_role = None
    while not ARRET.is_set():
        cfg = ram_config.get()

        # Filet périodique : rattrape ce qu'un envoi en échec aurait perdu.
        if time.time() - derniere_reprise > 300:
            derniere_reprise = time.time()
            recharger_file_notification(cfg)

        # Deux machines peuvent tourner en même temps (le Mac et le PC du
        # binôme). Seule l'instance principale notifie ; l'autre continue de
        # scanner et prendra le relais dès que la première s'arrête.
        role, raison = ram_telegram.role_instance(cfg)
        if role != dernier_role:
            dernier_role = role
            log("INFO", f"instance {role}", raison=raison)
        if role != "principal":
            ARRET.wait(20)
            continue
        if not ram_telegram.anti_spam_ok(cfg):
            ARRET.wait(2)
            continue

        with _verrou_notif:
            entree = heapq.heappop(_file_notif) if _file_notif else None
        if entree is None:
            ARRET.wait(2)
            continue

        score_negatif, annonce_id, _ = entree
        annonce = ram_db.get_annonce(annonce_id)
        if not annonce or annonce.get("statut") != "nouveau":
            continue

        pre = {"pre_score": -score_negatif,
               "revente_estimee": annonce.get("revente_estimee"),
               "marge_estimee": annonce.get("marge_estimee"),
               "drapeaux": _jlist(annonce.get("drapeaux"))}
        try:
            ram_telegram.notifier_etape1(annonce, pre, cfg)
            ram_db.maj_annonce(annonce_id, {"statut": "notifie"})
            STATS["notifiees"] += 1
            log("INFO", "notification envoyée", annonce=annonce_id,
                score=-score_negatif, marge=annonce.get("marge_estimee"),
                url=annonce.get("url"))
        except ram_telegram.TelegramError as e:
            STATS["erreurs"] += 1
            # L'annonce reste en statut « nouveau » : la reprise périodique la
            # remettra en file. Un échec d'envoi ne fait pas perdre l'affaire.
            log("ERROR", "notification en échec — sera retentée", annonce=annonce_id,
                erreur=str(e))


# ─────────────────────── WORKER VISION ───────────────────────
def worker_vision(cfg):
    """Dépile la file vision par score décroissant et enrichit la notification.

    Quota épuisé : l'annonce est différée (jamais perdue) et le message
    Telegram le dit explicitement. Les différées repassent en file dès que le
    quota se renouvelle.
    """
    if not cfg.val("vision.actif", True):
        log("INFO", "worker vision désactivé")
        return

    try:
        provider = ram_vision.provider_par_defaut(cfg)
    except ram_vision.VisionError as e:
        log("WARN", "worker vision non démarré", erreur=str(e))
        return
    if not provider.disponible():
        log("WARN", "worker vision non démarré : GEMINI_API_KEY absente")
        return

    log("INFO", "worker vision démarré", provider=provider.nom, modele=provider.modele)
    derniere_reprise = 0.0

    while not ARRET.is_set():
        cfg = ram_config.get()

        # Rattrapage périodique des différées (quota renouvelé).
        if time.time() - derniere_reprise > 60:
            derniere_reprise = time.time()
            reprises = ram_db.reprendre_differees(
                int(cfg.val("vision.reprise_differees_max", 3)))
            if reprises:
                log("INFO", "annonces différées remises en file", n=reprises)
            ram_db.purger_quota()

        tache = ram_db.prochaine_annonce_vision()
        if not tache:
            ARRET.wait(3)
            continue

        annonce_id = tache["annonce_id"]
        annonce = ram_db.get_annonce(annonce_id)
        if not annonce:
            ram_db.cloturer_vision(tache["file_id"], "abandonne", "annonce introuvable")
            continue

        try:
            resultat = ram_vision.analyser_annonce(annonce, provider, cfg)
        except Exception as e:
            STATS["erreurs"] += 1
            ram_db.cloturer_vision(tache["file_id"], "echec", str(e)[:200])
            log("ERROR", "analyse vision en échec", annonce=annonce_id, erreur=str(e))
            continue

        statut = resultat.get("statut")

        if statut == "quota":
            ram_db.differer_vision(tache["file_id"], resultat.get("erreur"))
            ram_db.maj_annonce(annonce_id, {"statut_verif": "quota_epuise"})
            try:
                ram_telegram.notifier_quota_epuise(annonce, cfg)
            except ram_telegram.TelegramError as e:
                log("WARN", "message quota non modifié", erreur=str(e))
            log("WARN", "quota vision épuisé, annonce différée", annonce=annonce_id)
            ARRET.wait(20)
            continue

        STATS["analysees"] += 1
        analyse = ram_parser.analyser(annonce.get("titre"), annonce.get("description"),
                                      annonce.get("nb_photos") or 0, cfg)
        fin = ram_scoring.score_final(annonce, analyse, resultat, cfg)

        ram_db.maj_annonce(annonce_id, {
            "score_final": fin.get("score_final"),
            "statut_verif": fin.get("statut_verif"),
            "marge_reelle": fin.get("marge_reelle"),
            "marge_reelle_pct": fin.get("marge_reelle_pct"),
            "revente_estimee": fin.get("revente_estimee") or annonce.get("revente_estimee"),
            "rejet_motif": fin.get("rejet_motif"),
            "drapeaux": fin.get("drapeaux"),
            "ref_id": (fin.get("ref") or {}).get("id") or annonce.get("ref_id"),
            "statut": "rejete" if fin.get("statut_verif") == "rejete"
                      else annonce.get("statut"),
        })
        ram_db.cloturer_vision(tache["file_id"], "fait")

        annonce = ram_db.get_annonce(annonce_id)
        seuil_confirme = float(cfg.val("scoring.seuil_confirme", 75))
        deja_notifiee = ram_db.notification_de_annonce(annonce_id) is not None

        # Une annonce sous le seuil de notification n'a pas été notifiée à
        # l'étape 1 : elle ne l'est à l'étape 2 que si le score final le mérite.
        if not deja_notifiee and fin.get("score_final", 0) < seuil_confirme:
            log("INFO", "analysée sans notification", annonce=annonce_id,
                score=fin.get("score_final"), statut=fin.get("statut_verif"))
            continue

        try:
            if deja_notifiee:
                ram_telegram.notifier_etape2(annonce, fin, resultat, cfg, fin.get("ref"))
            else:
                pre = {"pre_score": annonce.get("pre_score") or 0,
                       "revente_estimee": annonce.get("revente_estimee"),
                       "marge_estimee": annonce.get("marge_estimee"), "drapeaux": []}
                ram_telegram.notifier_etape1(annonce, pre, cfg)
                ram_telegram.notifier_etape2(annonce, fin, resultat, cfg, fin.get("ref"))
                ram_db.maj_annonce(annonce_id, {"statut": "notifie"})
                STATS["notifiees"] += 1
            log("INFO", "analyse terminée", annonce=annonce_id,
                statut=fin.get("statut_verif"), score=fin.get("score_final"),
                marge=fin.get("marge_reelle"), confiance=resultat.get("confiance"))
        except ram_telegram.TelegramError as e:
            STATS["erreurs"] += 1
            log("ERROR", "édition du message impossible", annonce=annonce_id,
                erreur=str(e))


# ─────────────────────── PLANIFICATEUR ───────────────────────
def worker_planificateur(cfg):
    """Job de calibrage quotidien + entretien (purge des annonces anciennes)."""
    dernier_calibrage = None
    while not ARRET.is_set():
        cfg = ram_config.get()
        heure_job = str(cfg.val("calibrage.heure_job", "04:30"))
        maintenant = datetime.now()
        aujourdhui = maintenant.strftime("%Y-%m-%d")

        if (maintenant.strftime("%H:%M") == heure_job and dernier_calibrage != aujourdhui):
            dernier_calibrage = aujourdhui
            log("INFO", "job de calibrage démarré")
            try:
                resultat = ram_calibration.job_quotidien(cfg, verbose=False)
                log("INFO", "job de calibrage terminé", **resultat)
            except Exception as e:
                log("ERROR", "job de calibrage en échec", erreur=str(e))
            try:
                purger(cfg)
            except Exception as e:
                log("WARN", "purge en échec", erreur=str(e))

        _ecrire_sante()          # battement régulier : prouve que le bot tourne
        ARRET.wait(30)


def purger(cfg=None):
    """Supprime les annonces anciennes jamais achetées. Les achetées et le
    journal des décisions ne sont JAMAIS purgés : ce sont eux qui permettent
    d'affiner le scoring dans la durée."""
    cfg = cfg or ram_config.get()
    jours = int(cfg.val("run.purge_annonces_jours", 90))
    limite = time.time() - jours * 86400
    with ram_db.get_db() as conn:
        curseur = conn.execute("""
            DELETE FROM ram_annonce
            WHERE detecte_le < ? AND statut NOT IN ('achete')
              AND id NOT IN (SELECT annonce_id FROM ram_stock WHERE annonce_id IS NOT NULL)
              AND id NOT IN (SELECT annonce_id FROM ram_journal_decision
                             WHERE annonce_id IS NOT NULL AND action='achat')
        """, (limite,))
        supprimees = curseur.rowcount
    log("INFO", "purge des annonces", supprimees=supprimees, plus_vieilles_que_j=jours)
    return supprimees


# ─────────────────────── ÉTAT ───────────────────────
def _jlist(valeur):
    if isinstance(valeur, list):
        return valeur
    try:
        out = json.loads(valeur or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def etat(cfg=None):
    cfg = cfg or ram_config.get()
    with _verrou_notif:
        file_notif = len(_file_notif)
    return {
        "actif": not ARRET.is_set(),
        "uptime_s": round(time.time() - STATS["demarre_le"], 1),
        "stats": dict(STATS),
        "workers": dict(SANTE),
        "file_notification": file_notif,
        "vision": ram_vision.etat_quota(cfg),
        "sources": ram_scrapers.etat_sources(cfg),
        "base": ram_db.stats_base(),
        "kpis": ram_db.kpis(),
        "calibrage": ram_calibration.alerte_calibrage(cfg),
        "config": {"notif_mode": cfg.notif_mode, "dry_run": cfg.dry_run,
                   "erreur_config": cfg.erreur},
        "secrets_manquants": ram_config.secrets_manquants(),
    }


def demarrer(cfg=None, une_seule_fois=False, avec_callbacks=True):
    """Démarre tous les workers. Retourne la liste des threads."""
    cfg = cfg or ram_config.get()
    ram_db.init_db()

    manquants = ram_config.secrets_manquants()
    if manquants and not cfg.dry_run:
        log("WARN", "secrets manquants — certaines fonctions seront inactives",
            manquants=manquants)

    threads = []
    cibles = [("scraping", worker_scraping, (cfg, une_seule_fois))]
    if not une_seule_fois:
        cibles += [
            ("notification", worker_notification, (cfg,)),
            ("vision", worker_vision, (cfg,)),
            ("planificateur", worker_planificateur, (cfg,)),
        ]
        if avec_callbacks and cfg.val("telegram.actif", True) and not cfg.dry_run:
            cibles.append(("callbacks", lambda c: ram_telegram.boucle_callbacks(cfg=c), (cfg,)))

    for nom, cible, args in cibles:
        t = threading.Thread(target=_superviser, args=(nom, cible, args),
                             name=f"ram-{nom}", daemon=True)
        t.start()
        threads.append(t)
    return threads


# Santé des workers, pour --etat et le dashboard.
SANTE = {}
SANTE_FICHIER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".ram_sante.json")


def _ecrire_sante():
    """Publie l'état des workers dans un fichier que le dashboard peut lire.

    Le dashboard tourne dans un AUTRE processus : sans ce fichier, il n'a aucun
    moyen de savoir qu'un worker est tombé. C'est précisément l'information qui
    manquait quand les notifications se sont arrêtées sans que rien ne le signale.
    """
    try:
        with open(SANTE_FICHIER, "w", encoding="utf-8") as f:
            json.dump({"maj_le": time.time(), "pid": os.getpid(),
                       "workers": SANTE, "stats": STATS}, f, ensure_ascii=False)
    except OSError:
        pass


def _superviser(nom, cible, args):
    """Relance un worker qui s'arrête sur une exception non prévue.

    Sans ça, une seule erreur imprévue — un délai réseau dépassé au mauvais
    moment, une réponse malformée — tuait le thread DÉFINITIVEMENT et en
    silence. Le worker de scraping, lui, continuait à tourner : les logs
    défilaient normalement pendant que plus aucune notification ne partait.
    Rien dans le terminal ne permettait de s'en rendre compte.

    Le worker vision et le planificateur peuvent se terminer normalement
    (vision désactivée, --once) : une sortie propre n'est pas un incident.
    """
    import traceback
    redemarrages = 0
    while not ARRET.is_set():
        SANTE[nom] = {"etat": "actif", "depuis": time.time(), "redemarrages": redemarrages}
        _ecrire_sante()
        try:
            cible(*args)
            SANTE[nom] = {"etat": "termine", "depuis": time.time(),
                          "redemarrages": redemarrages}
            _ecrire_sante()
            return
        except Exception as e:
            redemarrages += 1
            STATS["erreurs"] += 1
            SANTE[nom] = {"etat": "redemarrage", "depuis": time.time(),
                          "redemarrages": redemarrages, "derniere_erreur": str(e)}
            _ecrire_sante()
            log("ERROR", f"worker {nom} interrompu — redémarrage", erreur=str(e),
                type=type(e).__name__, redemarrages=redemarrages,
                trace=traceback.format_exc(limit=4))
            if ARRET.wait(min(5 * redemarrages, 60)):
                return


# Options reconnues. Une option inconnue doit ARRÊTER le programme : sur une
# version pas encore à jour, « --dashboard » était silencieusement ignoré et le
# bot complet démarrait à la place — impossible à comprendre depuis le terminal.
OPTIONS = {
    "--dry-run": "n'envoie aucune notification",
    "--once": "un seul tour de scan puis sortie",
    "--dashboard": "sert le dashboard web (défaut : port 8010)",
    "--diag": "explique pourquoi les annonces sont rejetées",
    "--replay": "rejoue les annonces archivées à travers le scoring courant",
    "--calibrer": "lance le job de calibrage des prix",
    "--etat": "état complet du système, en JSON",
    "--seed": "recharge la base de références",
    "--force": "démarre même si une autre instance tourne déjà",
    "--limite=N": "nombre d'annonces à traiter (--diag, --replay)",
    "--port=N": "port du dashboard",
}


def _valider_options(args, silencieux=False):
    inconnues = [a for a in args
                 if a not in OPTIONS
                 and not a.startswith(("--limite=", "--port="))]
    if not inconnues:
        return True
    if silencieux:
        return False
    print(f"\n❌ Option inconnue : {' '.join(inconnues)}\n")
    print("Options disponibles :")
    for opt, desc in OPTIONS.items():
        print(f"  {opt:<16} {desc}")
    print("\nSi l'option devrait exister, le dépôt local n'est pas à jour :")
    print("    git pull origin main\n")
    return False


def _verrou_instance():
    """Empêche deux exécutions simultanées du bot.

    Deux instances, c'est deux fois plus de requêtes vers Vinted — exactement
    ce qui mène au bannissement qu'on cherche à éviter — et un conflit Telegram
    (HTTP 409 : l'API n'autorise qu'un seul getUpdates par bot).

    Retourne le descripteur du verrou (à garder ouvert) ou None si occupé.
    """
    import fcntl
    chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ram_sniper.lock")
    try:
        fd = open(chemin, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except (OSError, BlockingIOError):
        return None


def main():
    args = sys.argv[1:]
    if not _valider_options(args):
        sys.exit(2)
    if "--dry-run" in args:
        os.environ["RAM_DRY_RUN"] = "1"
    cfg = ram_config.get(force=True)
    ram_db.init_db()

    if "--etat" in args:
        print(json.dumps(etat(cfg), indent=2, ensure_ascii=False, default=str))
        return

    if "--dashboard" in args:
        # Dashboard autonome : ne dépend que de Flask, pas du reste de SOLDIER
        # (app.py tire lbc, curl_cffi, ebay… — une seule de ces dépendances
        # absente et le dashboard devenait inaccessible).
        port = 8010
        for a in args:
            if a.startswith("--port="):
                port = int(a.split("=", 1)[1])
        try:
            from flask import Flask
        except ImportError:
            print("Flask absent : venv/bin/pip install flask")
            return
        import ram_routes
        appli = Flask(__name__)
        ram_routes.enregistrer(appli)
        url = f"http://127.0.0.1:{port}/ram"
        print(f"\n🎯 Dashboard RAM SNIPER : {url}\n   Ctrl+C pour arrêter\n")
        try:
            import webbrowser
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
        appli.run(host="127.0.0.1", port=port, threaded=True, debug=False)
        return

    if "--diag" in args:
        limite = 500
        for a in args:
            if a.startswith("--limite="):
                limite = int(a.split("=", 1)[1])
        ram_scrapers.diagnostic(limite, cfg)
        return

    if "--replay" in args:
        limite = 500
        for a in args:
            if a.startswith("--limite="):
                limite = int(a.split("=", 1)[1])
        ram_scrapers.rejouer(limite, cfg)
        return

    if "--calibrer" in args:
        ram_calibration.job_quotidien(cfg)
        return

    if "--seed" in args:
        ram_db.seed_references()
        return

    # Verrou d'instance : pris seulement pour une vraie exécution du bot.
    # Le dashboard, --diag et --replay peuvent tourner en parallèle sans risque.
    verrou = _verrou_instance()
    if verrou is None and "--force" not in args:
        print("\n⚠️  Un RAM SNIPER tourne déjà.\n")
        print("  Deux instances doublent les requêtes vers Vinted (risque de")
        print("  bannissement) et se disputent l'API Telegram (erreur HTTP 409).\n")
        print("  • Pour voir le dashboard, c'est une AUTRE commande :")
        print("        venv/bin/python3 ram_sniper.py --dashboard\n")
        print("  • Pour arrêter l'instance en cours : Ctrl+C dans son terminal")
        print("  • Pour passer outre malgré tout : --force\n")
        sys.exit(1)

    print("═" * 68)
    print("  🎯 RAM SNIPER — DDR4 UDIMM desktop")
    print("═" * 68)
    etat_sources = ram_scrapers.etat_sources(cfg)
    for nom, e in etat_sources.items():
        symbole = "✅" if (e["actif"] and e["client"]) else ("⚠️" if e["actif"] else "⏸")
        print(f"  {symbole} {nom:<11} {e['mots_cles']} mots-clés"
              + (f"  — {e['erreur']}" if e.get("erreur") else ""))
    quota = ram_vision.etat_quota(cfg)
    print(f"  {'✅' if quota['disponible'] else '⏸'} vision      "
          f"{quota['provider']}/{quota['modele']} — "
          f"{quota['restant_jour']}/{quota['plafond_jour']} restants aujourd'hui")
    print(f"  {'🔕' if cfg.dry_run else '🔔'} telegram    mode {cfg.notif_mode}"
          + ("  (DRY-RUN : aucune notification ne sera envoyée)" if cfg.dry_run else ""))
    print(f"  📊 dashboard   AUTRE terminal : "
          f"venv/bin/python3 ram_sniper.py --dashboard")
    base = ram_db.stats_base()
    print(f"  📚 références  {base['references']} "
          f"({', '.join(f'{t}:{n}' for t, n in sorted(base['references_par_tier'].items()))})")
    manquants = ram_config.secrets_manquants()
    if manquants:
        print(f"  ⚠️  secrets manquants : {', '.join(manquants)}")
    print("═" * 68)
    print("  Ctrl+C pour arrêter\n")

    def arreter(signum, frame):
        print("\n⏹  arrêt en cours…")
        ARRET.set()

    signal.signal(signal.SIGINT, arreter)
    signal.signal(signal.SIGTERM, arreter)

    threads = demarrer(cfg, une_seule_fois="--once" in args)
    try:
        if "--once" in args:
            for t in threads:
                t.join()
            print(json.dumps(STATS, indent=2, ensure_ascii=False, default=str))
        else:
            while not ARRET.is_set():
                time.sleep(1)
    except KeyboardInterrupt:
        ARRET.set()

    print(f"\n📊 {STATS['annonces_nouvelles']} nouvelle(s) annonce(s), "
          f"{STATS['notifiees']} notification(s), {STATS['analysees']} analyse(s), "
          f"{STATS['rejetees']} rejet(s), {STATS['appariements']} appariement(s)")


if __name__ == "__main__":
    main()
