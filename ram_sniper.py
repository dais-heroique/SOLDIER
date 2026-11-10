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
import ram_composants
import ram_config
import ram_db
import ram_pairing
import ram_parser
import ram_scoring
import ram_scrapers
import ram_telegram
import ram_vision
import reseau

ARRET = threading.Event()
# Arrêt du scan seul : en fin de session continue, on cesse de chercher mais
# le worker de notification finit d'envoyer ce qui est en file.
ARRET_SCAN = threading.Event()
_reveil_notif = threading.Event()        # une annonce vient d'entrer en file
_essais_notif = {}                       # annonce -> envois Telegram ratés d'affilée
_verrou_composants = threading.Lock()    # un seul envoi « composants » à la fois

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
    _reveil_notif.set()      # réveille le worker tout de suite (pas d'attente de 2 s)


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


def worker_scraping(cfg, une_seule_fois=False, limite_mots=None):
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
        reseau.debut_cycle()
        cycle = {"vues": 0, "retenues": 0, "meilleur": 0.0, "meilleur_titre": None}
        for scraper in scrapers:
            if ARRET.is_set():
                break
            scraper.echecs_consecutifs = 0
            mots = scraper.mots_cles()
            random.shuffle(mots)
            if limite_mots:
                mots = mots[:limite_mots]
            seuil_disj = int(cfg.val(f"sources.{scraper.source}.disjoncteur_echecs", 4))
            for mot in mots:
                if ARRET.is_set():
                    break
                # Disjoncteur : N échecs d'affilée = on est bloqué. Insister ne
                # ferait qu'aggraver le blocage (et gaspiller le temps du run).
                if seuil_disj and getattr(scraper, "echecs_consecutifs", 0) >= seuil_disj:
                    log("WARN", "disjoncteur : source mise de côté pour ce cycle",
                        source=scraper.source, echecs=scraper.echecs_consecutifs)
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

            # Processeurs, cartes graphiques, cartes mères : même source, même
            # sortie réseau, même disjoncteur. Une panne ici ne doit jamais
            # empêcher le scan RAM ni la fin du passage.
            if cfg.val("composants.actif", True) and not ARRET.is_set() and not (
                    seuil_disj and scraper.echecs_consecutifs >= seuil_disj):
                try:
                    evals = ram_composants.scanner_source(
                        scraper, cfg, arret=ARRET)
                    STATS["annonces_vues"] += len(evals)
                except Exception as e:
                    STATS["erreurs"] += 1
                    log("ERROR", "scan composants en échec", source=scraper.source,
                        erreur=str(e))

        try:
            ram_composants.notifier(cfg)
        except Exception as e:
            log("WARN", "notifications composants en échec", erreur=str(e))

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

        alerter_sorties(cfg, [s.source for s in scrapers])

        if une_seule_fois:
            break
        ARRET.wait(5)


# ─────────────────────── SCAN CONTINU (un fil par source) ───────────────────────
RAPIDE_DEFAUT_S = {"vinted": [12, 20], "leboncoin": [25, 40]}
PAUSES_BLOCAGE_S = [60, 120, 300, 600]
BILAN_TOUTES_LES_S = 120


def _scan_fini():
    return ARRET.is_set() or ARRET_SCAN.is_set()


def _attendre_scan(secondes):
    """Attente interruptible par l'un ou l'autre des deux signaux d'arrêt."""
    fin = time.time() + secondes
    while not _scan_fini() and time.time() < fin:
        time.sleep(min(1.0, max(0.05, fin - time.time())))


def _cadences(valeur, defaut):
    """[(mot, lo, hi)] depuis une liste (cadence par défaut) ou un dict
    {mot: [lo, hi]} (cadence propre à chaque recherche)."""
    lo0, hi0 = float(defaut[0]), float(defaut[1])
    sortie = []
    if isinstance(valeur, dict):
        for mot, plage in valeur.items():
            try:
                lo, hi = float(plage[0]), float(plage[1])
            except (TypeError, ValueError, IndexError):
                lo, hi = lo0, hi0
            sortie.append((str(mot), min(lo, hi), max(lo, hi)))
    else:
        sortie = [(str(m), lo0, hi0) for m in (valeur or [])]
    return sortie


def cadences_rapides(scraper, cfg):
    """Voie rapide RAM : recherches larges, triées « plus récent d'abord »,
    où toute nouvelle annonce apparaît en tête."""
    source = scraper.source
    defaut = cfg.val(f"sources.{source}.rapide_toutes_les_s",
                     RAPIDE_DEFAUT_S.get(source, [20, 30]))
    liste = _cadences(cfg.val(f"sources.{source}.mots_cles_rapides", None), defaut)
    if not liste:
        liste = _cadences(scraper.mots_cles()[:1], defaut)
    return liste


def cadences_larges(source, cfg):
    """Recherches larges « composants » (rtx, ryzen…) et leur cadence."""
    if not cfg.val("composants.actif", True):
        return []
    propres = cfg.val(f"sources.{source}.composants_larges", None)
    if propres:
        return _cadences(propres, [120, 200])
    r = ram_composants.reglages(cfg)
    return _cadences(r.get("requetes_larges"),
                     (r.get("larges_toutes_les_s") or {}).get(source, [120, 200]))


def mots_rapides(scraper, cfg):
    return [m for m, _, _ in cadences_rapides(scraper, cfg)]


class Tournee:
    """Ordre des recherches d'une source en mode continu.

    Chaque recherche fréquente a SA cadence ; à chaque créneau on sert la
    plus en retard :
      • rapide — RAM (« ddr4 » toutes les ~10 s) : une annonce qui vient
        d'être publiée est vue en quelques secondes ;
      • large — cartes graphiques, processeurs, cartes mères : « rtx »,
        « ryzen »… voient passer TOUS les modèles, lus ensuite dans le titre ;
      • lente — les autres mots-clés et les recherches par modèle, à tour de
        rôle dans les créneaux libres (profondeur : annonces de la nuit,
        titres atypiques). Un créneau sur MAX_RAPIDES_D_AFFILEE + 1 lui est
        garanti, même si les voies fréquentes sont saturées.
    """

    MAX_RAPIDES_D_AFFILEE = 5

    def __init__(self, scraper, horloge=time.time):
        self.scraper = scraper
        self.horloge = horloge
        self._echeance = {}      # (genre, mot) -> prochain passage
        self._lente = []
        self._d_affilee = 0

    def _frequentes(self, cfg):
        source = self.scraper.source
        return ([("ram", m, lo, hi) for m, lo, hi in cadences_rapides(self.scraper, cfg)]
                + [("large", m, lo, hi) for m, lo, hi in cadences_larges(source, cfg)])

    def suivante(self, cfg):
        """("ram", mot), ("large", recherche composants) ou ("composant", None)."""
        maintenant = self.horloge()
        frequentes = self._frequentes(cfg)
        meilleur, retard_max = None, -1.0
        for genre, mot, lo, hi in frequentes:
            cle = (genre, mot)
            if cle not in self._echeance:
                # RAM : tout de suite. Composants : premier passage étalé,
                # pas de rafale au démarrage.
                self._echeance[cle] = maintenant + (
                    0 if genre == "ram" else random.uniform(0, hi))
            retard = (maintenant - self._echeance[cle]) / max(hi, 1.0)
            if retard >= 0 and retard > retard_max:
                meilleur, retard_max = (genre, mot, lo, hi), retard
        if meilleur and self._d_affilee < self.MAX_RAPIDES_D_AFFILEE:
            genre, mot, lo, hi = meilleur
            self._echeance[(genre, mot)] = maintenant + random.uniform(lo, hi)
            self._d_affilee += 1
            return genre, mot
        self._d_affilee = 0
        if not self._lente:
            rapides = {m for g, m, _, _ in frequentes if g == "ram"}
            lents = [("ram", m) for m in self.scraper.mots_cles() if m not in rapides]
            if cfg.val("composants.actif", True):
                n = int(ram_composants.reglages(cfg)["requetes_par_source"])
                lents += [("composant", None)] * max(0, n)
            random.shuffle(lents)      # pas de motif régulier côté anti-bot
            self._lente = lents or [("ram", m) for m in rapides]
        if not self._lente:            # source sans aucun mot-clé
            return (frequentes[0][0], frequentes[0][1]) if frequentes else ("composant", None)
        return self._lente.pop()


def worker_source(scraper, cfg):
    """Scan continu d'UNE source. Chaque source a son fil : Leboncoin, plus
    lent, ne retarde plus Vinted. Les annonces partent en notification une par
    une, dès qu'elles sont évaluées."""
    source = scraper.source
    tournee = Tournee(scraper)
    blocages = 0
    dernier_bilan = time.time()
    reseau.debut_cycle(source)
    log("INFO", "scan continu démarré", source=source,
        rapides=mots_rapides(scraper, cfg))

    mesure = {"n": 0, "duree": 0.0, "nouvelles": 0}

    def bilan():
        alerter_sorties(ram_config.get(), [source])
        sortie = (reseau.bilan_cycle().get(source) or {}).get("ok")
        reseau.debut_cycle(source)
        # Filet : une affaire « composants » dont l'envoi Telegram a échoué
        # repart ici, sans attendre qu'une autre affaire se présente.
        try:
            with _verrou_composants:
                ram_composants.notifier(ram_config.get())
        except Exception as e:
            log("WARN", "notifications composants en échec", erreur=str(e))
        if mesure["n"]:
            # Le rythme réel, visible dans les logs : c'est lui qui dit si le
            # bot est aussi rapide que prévu.
            log("INFO", "rythme", source=source, recherches=mesure["n"],
                duree_moy_s=round(mesure["duree"] / mesure["n"], 2),
                nouvelles=mesure["nouvelles"],
                sortie=sortie)
            mesure.update(n=0, duree=0.0, nouvelles=0)

    while not _scan_fini():
        cfg = ram_config.get()
        seuil_disj = int(cfg.val(f"sources.{source}.disjoncteur_echecs", 4))
        if seuil_disj and scraper.echecs_consecutifs >= seuil_disj:
            # Bloqué partout : insister aggrave le blocage. On attend, de plus
            # en plus longtemps, puis on retente ; l'alerte Telegram part d'ici.
            pause = PAUSES_BLOCAGE_S[min(blocages, len(PAUSES_BLOCAGE_S) - 1)]
            blocages += 1
            log("WARN", "disjoncteur : source en pause", source=source,
                echecs=scraper.echecs_consecutifs, pause_s=pause)
            bilan()
            dernier_bilan = time.time()
            scraper.echecs_consecutifs = 0
            _attendre_scan(pause)
            continue

        genre, mot = tournee.suivante(cfg)
        t0 = time.time()
        try:
            if genre == "composant":
                # scanner_source fait sa propre pause après la requête.
                evals = ram_composants.scanner_source(scraper, cfg, limite=1,
                                                      arret=ARRET_SCAN)
                STATS["annonces_vues"] += len(evals)
            elif genre == "large":
                evals = ram_composants.scanner_large(scraper, mot, cfg)
                STATS["annonces_vues"] += len(evals)
            else:
                evals = None
                resultats = scraper.scanner_mot_cle(
                    mot, sur_resultat=lambda r: traiter_resultats([r], cfg))
                STATS["annonces_vues"] += len(resultats)
                nouvelles = sum(1 for r in resultats if r["nouvelle"])
                mesure["nouvelles"] += nouvelles
                if nouvelles:
                    log("INFO", "scan", source=source, mot_cle=mot,
                        vues=len(resultats), nouvelles=nouvelles)
            if evals and any(e.get("statut") == "a_notifier" for e in evals):
                with _verrou_composants:
                    ram_composants.notifier(cfg)
            STATS["dernier_scan"] = time.time()
        except Exception as e:
            STATS["erreurs"] += 1
            scraper.echecs_consecutifs = getattr(scraper, "echecs_consecutifs", 0) + 1
            log("ERROR", "scan en échec", source=source, mot_cle=mot, erreur=str(e))
        mesure["n"] += 1
        mesure["duree"] += time.time() - t0
        if scraper.echecs_consecutifs == 0:
            blocages = 0

        if time.time() - dernier_bilan >= BILAN_TOUTES_LES_S:
            dernier_bilan = time.time()
            STATS["cycles"] += 1
            bilan()
        if genre != "composant" and not _scan_fini():
            lo, hi = cfg.val(f"sources.{source}.delai_entre_requetes_s", [2.5, 5.0])
            _attendre_scan(random.uniform(float(lo), float(hi)))


def cibles_scan_continu(cfg):
    """Un worker supervisé par source active."""
    cibles = []
    if cfg.val("sources.vinted.actif", True):
        cibles.append(("scan-vinted", worker_source, (ram_scrapers.ScraperVinted(cfg), cfg)))
    if cfg.val("sources.leboncoin.actif", True):
        cibles.append(("scan-leboncoin", worker_source,
                       (ram_scrapers.ScraperLeboncoin(cfg), cfg)))
    if not cibles:
        log("WARN", "aucune source active")
    return cibles


# ─────────────────────── ALERTES RÉSEAU ET SIGNE DE VIE ───────────────────────
NOMS_SOURCES = {"vinted": "Vinted", "leboncoin": "Leboncoin"}


def sources_actives(cfg):
    return [s for s in ("vinted", "leboncoin") if cfg.val(f"sources.{s}.actif", True)]


def message_alerte(source, type_alerte, sortie=None):
    nom = NOMS_SOURCES.get(source, source)
    noms = [s.nom for s in reseau.sorties()]
    if type_alerte == "bloque":
        texte = (f"🔴 <b>{nom} bloqué</b> sur toutes les sorties réseau "
                 f"({', '.join(noms)}).\nLe bot réessaie tout seul, sans marteler. ")
        if len(noms) == 1:
            texte += ("Une seule sortie est configurée : ajoute la sortie maison "
                      "(Tailscale, gratuit) — README §10.")
        else:
            texte += "Si ça dure, vérifie que l'appareil de la sortie maison est allumé."
        return texte
    if type_alerte == "retabli":
        return f"🟢 <b>{nom} rétabli</b> (sortie : {sortie}). La surveillance reprend normalement."
    return (f"🟠 {nom} : la connexion directe est bloquée, le bot passe par la sortie "
            f"<b>{sortie}</b>. Rien à faire.")


def alerter_sorties(cfg, sources):
    """Prévient sur Telegram quand une source est bloquée partout, quand elle
    revient, et quand une sortie de secours prend le relais. Un raté isolé
    n'alerte pas (alertes.cycles_avant_alerte)."""
    seuil = int(cfg.val("alertes.cycles_avant_alerte", 2))
    for source in sources:
        try:
            alerte = reseau.evaluer_alertes(source, seuil)
        except Exception as e:
            log("WARN", "évaluation des alertes réseau impossible", erreur=str(e))
            continue
        if not alerte:
            continue
        type_alerte, sortie = alerte
        log("WARN" if type_alerte == "bloque" else "INFO", "alerte réseau",
            source=source, alerte=type_alerte, sortie=sortie)
        try:
            if ram_telegram.est_principal(cfg):
                ram_telegram.envoyer_texte(message_alerte(source, type_alerte, sortie), cfg,
                                           silencieux=(type_alerte == "secours"))
        except ram_telegram.TelegramError as e:
            log("WARN", "alerte réseau non envoyée", erreur=str(e))


def message_signe_de_vie(sources, premier=False):
    depuis = time.time() - 86400
    with ram_db.get_db() as conn:
        def compter(condition=""):
            return conn.execute("SELECT COUNT(*) n FROM ram_annonce WHERE detecte_le > ? "
                                + condition, (depuis,)).fetchone()["n"]
        vues, retenues = compter(), compter("AND exclusion IS NULL")
        sans_avis = compter("AND exclusion = 'vendeur_sans_avis'")
    notifiees = len(ram_db.notifications_recentes(86400))
    if premier:
        lignes = ["✅ <b>RAM Sniper en ligne</b> — premier passage réussi.",
                  "Tu recevras ce résumé une fois par jour."]
    else:
        lignes = ["✅ <b>RAM Sniper en ligne</b> — dernières 24 h",
                  f"Annonces vues : {vues} · retenues : {retenues} · notifiées : {notifiees}",
                  f"Écartées (vendeur sans avis) : {sans_avis}"]
    lignes.append("Sorties réseau :")
    for src in sources:
        lignes.append(f"• {NOMS_SOURCES.get(src, src)} : "
                      f"{ram_telegram._echapper(reseau.resume_texte(src))}")
    lignes.append("<i>Pas de résumé demain = le bot ne tourne plus : regarde l'onglet "
                  "Actions sur GitHub.</i>")
    return "\n".join(lignes)


def signe_de_vie(cfg, sources=None):
    """Un résumé silencieux par jour. Son ABSENCE est l'alerte : un bot arrêté
    (cron désactivé, GitHub en panne) ne peut pas prévenir lui-même."""
    heures = float(cfg.val("alertes.signe_de_vie_h", 24) or 0)
    if heures <= 0 or not ram_config.secret("TELEGRAM_BOT_TOKEN"):
        return False
    v = reseau.vie()
    dernier = float(v.get("signe_de_vie_le") or 0)
    if dernier and time.time() - dernier < heures * 3600:
        return False
    sources = sources or sources_actives(cfg)
    try:
        if not ram_telegram.est_principal(cfg):
            return False
        ram_telegram.envoyer_texte(message_signe_de_vie(sources, premier=not dernier),
                                   cfg, silencieux=True)
    except ram_telegram.TelegramError as e:
        log("WARN", "signe de vie non envoyé — nouvel essai au prochain passage",
            erreur=str(e))
        return False
    v["signe_de_vie_le"] = time.time()
    reseau.sauver()
    return True


def maintenance_quotidienne(cfg):
    """En ligne, il n'y a pas de planificateur permanent : calibrage des prix et
    purge passent une fois par jour, au premier passage après calibrage.heure_job.
    Le calibrage est borné (ci.calibrage_annonces) et passe par une sortie qui
    marche, pour ne pas griller l'IP en rafale."""
    v = reseau.vie()
    maintenant = datetime.now()
    aujourdhui = maintenant.strftime("%Y-%m-%d")
    if v.get("maintenance_le") == aujourdhui or \
            maintenant.strftime("%H:%M") < str(cfg.val("calibrage.heure_job", "04:30")):
        return False
    v["maintenance_le"] = aujourdhui
    reseau.sauver()
    try:
        purger(cfg)
    except Exception as e:
        log("WARN", "purge en échec", erreur=str(e))
    if not cfg.val("calibrage.actif", True):
        return True
    try:
        sortie = reseau.choisir("vinted")
        n = 0
        if sortie is not None and ram_scrapers._HAS_VINTED:
            client = ram_scrapers.VintedClient(country="FR", proxy=sortie.url)
            n = ram_calibration.collecter_vinted_vendus(
                cfg=cfg, verbose=False, client=client,
                limite_annonces=int(cfg.val("ci.calibrage_annonces", 30)))
        changements = ram_calibration.recalibrer(cfg=cfg, verbose=False)
        log("INFO", "maintenance quotidienne", observations=n,
            references_recalibrees=len(changements),
            sortie=sortie.nom if sortie else None)
    except Exception as e:
        log("WARN", "calibrage en échec", erreur=str(e))
    return True


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
            ARRET.wait(1)
            continue

        with _verrou_notif:
            entree = heapq.heappop(_file_notif) if _file_notif else None
        if entree is None:
            _reveil_notif.wait(2)
            _reveil_notif.clear()
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
            # Nouvel essai rapide (3 s, 6 s, 12 s…) : attendre la reprise
            # périodique (5 min) ferait arriver l'alerte trop tard. Au-delà de
            # 5 essais l'annonce reste « nouveau » et la reprise la reprendra.
            essais = _essais_notif.get(annonce_id, 0) + 1
            _essais_notif[annonce_id] = essais
            log("ERROR", "notification en échec — sera retentée", annonce=annonce_id,
                essai=essais, erreur=str(e))
            if essais <= 5:
                ARRET.wait(min(3 * 2 ** (essais - 1), 30))
                with _verrou_notif:
                    heapq.heappush(_file_notif, entree)
            else:
                _essais_notif.pop(annonce_id, None)
            continue
        _essais_notif.pop(annonce_id, None)


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

        try:
            signe_de_vie(cfg)
        except Exception as e:
            log("WARN", "signe de vie en échec", erreur=str(e))
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
    try:
        log("INFO", "purge des composants", supprimes=ram_composants.purger(cfg))
    except Exception as e:
        log("WARN", "purge des composants en échec", erreur=str(e))
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
        "sorties_reseau": {"ordre": [x.nom for x in reseau.sorties()],
                           "etat": reseau.resume()},
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
    if une_seule_fois:
        cibles = [("scraping", worker_scraping, (cfg, une_seule_fois))]
    else:
        cibles = cibles_scan_continu(cfg)
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


def _requetes_et_erreurs():
    with ram_db.get_db() as conn:
        ligne = conn.execute("SELECT COALESCE(SUM(requetes),0) r, "
                             "COALESCE(SUM(erreurs),0) e FROM ram_scan_stat").fetchone()
    return int(ligne["r"]), int(ligne["e"])


# ─────────────────────── PLAGE HORAIRE ───────────────────────
def plage_horaire(cfg=None):
    """(début, fin) en heures entières, ou None = toujours actif.

    SNIPER_HEURES (« 7-23 », variable GitHub du même nom) prend le pas sur
    ci.heures_actives : on règle la plage depuis l'interface GitHub, sans
    commit. « 0-24 » ou une valeur vide désactive la limite. Fin exclue :
    « 7-23 » = de 07:00 à 22:59.
    """
    cfg = cfg or ram_config.get()
    brut = os.environ.get("SNIPER_HEURES")
    if brut is not None and brut.strip():
        try:
            debut, fin = (int(x) for x in brut.replace(" ", "").split("-"))
        except ValueError:
            log("WARN", "SNIPER_HEURES illisible, plage ignorée", valeur=brut)
            return None
    else:
        valeur = cfg.val("ci.heures_actives", None)
        if not valeur or len(valeur) != 2:
            return None
        debut, fin = int(valeur[0]), int(valeur[1])
    if debut == fin or (debut <= 0 and fin >= 24):
        return None
    return debut, fin


def dans_plage_horaire(cfg=None, maintenant=None):
    """Vrai si le bot doit travailler à cette heure (fuseau ci.fuseau, Paris).

    Le cron GitHub est en UTC : l'heure d'été ferait glisser une plage écrite
    en UTC d'une heure deux fois par an. On convertit donc ici, et le cron du
    workflow reste volontairement large. SNIPER_IGNORER_PLAGE=1 (lancement
    manuel) force le passage.
    """
    cfg = cfg or ram_config.get()
    if os.environ.get("SNIPER_IGNORER_PLAGE"):
        return True
    plage = plage_horaire(cfg)
    if plage is None:
        return True
    debut, fin = plage
    fuseau = str(cfg.val("ci.fuseau", "Europe/Paris"))
    try:
        from zoneinfo import ZoneInfo
        heure = (maintenant or datetime.now(ZoneInfo(fuseau))).astimezone(ZoneInfo(fuseau)).hour
    except Exception as e:
        log("WARN", "fuseau horaire indisponible, heure locale utilisée",
            fuseau=fuseau, erreur=str(e))
        heure = (maintenant or datetime.now()).hour
    if debut < fin:
        return debut <= heure < fin
    return heure >= debut or heure < fin        # plage qui passe minuit (22-6)



def executer_ci(cfg=None):
    """Un passage complet pour un hébergement par cron (GitHub Actions) :
    scan de quelques mots-clés par source, envoi des notifications en attente,
    analyses vision si une clé Gemini existe, puis sortie propre.

    Chaque requête bloquée bascule sur la sortie réseau suivante (reseau.py).
    Quand TOUT est bloqué, l'alerte part sur Telegram (après
    alertes.cycles_avant_alerte passages) et le run reste vert — un run rouge
    toutes les 10 min, c'est un e-mail de GitHub toutes les 10 min. Code 3
    seulement si Telegram n'est pas configuré (seul signal restant) ou si
    ci.echec_si_bloque est vrai.
    """
    cfg = cfg or ram_config.get()
    if not dans_plage_horaire(cfg):
        log("INFO", "hors plage horaire : passage ignoré", plage=plage_horaire(cfg))
        return 0
    ram_db.init_db()
    os.environ["RAM_SANS_ELECTION"] = "1"     # une seule instance en ligne

    limite_mots = int(cfg.val("ci.mots_cles_par_source", 8))
    budget = float(cfg.val("ci.budget_notifications_s", 240))
    manquants = ram_config.secrets_manquants()
    if manquants and not cfg.dry_run:
        log("WARN", "secrets manquants", manquants=manquants)

    vision_ok = False
    if cfg.val("vision.actif", True):
        try:
            vision_ok = bool(ram_vision.provider_par_defaut(cfg).disponible())
        except Exception:
            vision_ok = False

    log("INFO", "sorties réseau", ordre=[x.nom for x in reseau.sorties()])
    req0, err0 = _requetes_et_erreurs()
    ARRET.clear()
    cibles = [("notification", worker_notification, (cfg,))]
    if vision_ok:
        cibles.append(("vision", worker_vision, (cfg,)))
    fils = []
    for nom, cible, args in cibles:
        t = threading.Thread(target=_superviser, args=(nom, cible, args),
                             name=f"ram-{nom}", daemon=True)
        t.start()
        fils.append(t)

    worker_scraping(cfg, une_seule_fois=True, limite_mots=limite_mots)

    # Laisser partir les notifications (anti-spam compris) et finir la vision,
    # dans la limite du budget : le reste sera repris au passage suivant.
    fin = time.time() + budget
    while time.time() < fin:
        with _verrou_notif:
            en_attente = len(_file_notif)
        etat_vision = ram_db.etat_file_vision() if vision_ok else {}
        vision_restante = etat_vision.get("en_attente", 0) + etat_vision.get("en_cours", 0)
        if en_attente == 0 and vision_restante == 0:
            break
        time.sleep(2)
    ARRET.set()
    for t in fils:
        t.join(timeout=10)

    for tache in (lambda: signe_de_vie(cfg), lambda: maintenance_quotidienne(cfg)):
        try:
            tache()
        except Exception as e:
            log("WARN", "tâche périodique en échec", erreur=str(e))

    try:    # fusionne le journal WAL : la base est ensuite un fichier unique
        with ram_db.get_db() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        log("WARN", "checkpoint de la base impossible", erreur=str(e))

    reseau.sauver()

    req1, err1 = _requetes_et_erreurs()
    requetes, erreurs = req1 - req0, err1 - err0
    bilan = reseau.bilan_cycle()
    essayees = [src for src, c in bilan.items() if c["ok"] or c["echecs"]]
    reussies = {src: c["ok"] for src, c in bilan.items() if c["ok"]}
    log("INFO", "passage terminé", requetes=requetes, erreurs=erreurs,
        nouvelles=STATS["annonces_nouvelles"], notifiees=STATS["notifiees"],
        rejetees=STATS["rejetees"], sorties_utilisees=reussies,
        sources_bloquees=[src for src in essayees if src not in reussies])
    tout_bloque = bool(essayees and not reussies) or (requetes > 0 and erreurs >= requetes)
    if tout_bloque:
        log("ERROR", "toutes les sources sont bloquées sur toutes les sorties réseau. "
            "Lance « Test réseau » et ajoute une sortie maison (README §10).",
            sorties=[x.nom for x in reseau.sorties()])
        telegram_ok = bool(ram_config.secret("TELEGRAM_BOT_TOKEN")
                           and ram_config.secret("TELEGRAM_CHAT_ID"))
        if cfg.val("ci.echec_si_bloque", False) or not telegram_ok:
            return 3
    return 0


# Santé des workers, pour --etat et le dashboard.
# ─────────────────────── SESSION CONTINUE (hébergement en ligne) ───────────────────────
def _checkpoint_base():
    """Fusionne le journal WAL : seul soldier.db est sauvegardé en ligne."""
    try:
        with ram_db.get_db() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        log("WARN", "checkpoint de la base impossible", erreur=str(e))


def _publier_relais(valeur):
    """Dit au workflow GitHub s'il doit relancer une session derrière celle-ci."""
    chemin = os.environ.get("GITHUB_OUTPUT")
    if not chemin:
        return
    try:
        with open(chemin, "a", encoding="utf-8") as f:
            f.write(f"relais={'true' if valeur else 'false'}\n")
    except OSError:
        pass


def executer_continu(cfg=None, duree_s=None, pas_s=1.0):
    """Session longue pour GitHub Actions : le bot tourne SANS ARRÊT, comme
    sur un ordinateur (un fil par source, voie rapide, notifications
    immédiates), au lieu d'un passage toutes les 10 minutes.

    Un job GitHub est limité à 6 h : la session s'arrête proprement avant
    (ci.duree_session_min), sauvegarde, et demande au workflow de lancer la
    suivante (relais). Elle s'arrête aussi à la sortie de la plage horaire.
    Un lancement manuel hors plage sert de test : ci.duree_test_min, sans relais.
    """
    cfg = cfg or ram_config.get()
    force = bool(os.environ.pop("SNIPER_IGNORER_PLAGE", None))
    en_plage = dans_plage_horaire(cfg)
    if not en_plage and not force:
        log("INFO", "hors plage horaire : session non démarrée", plage=plage_horaire(cfg))
        _publier_relais(False)
        return 0
    if duree_s is None:
        env = os.environ.get("SNIPER_DUREE_MIN")
        minutes = float(env) if env else float(
            cfg.val("ci.duree_session_min", 320) if en_plage
            else cfg.val("ci.duree_test_min", 15))
        duree_s = minutes * 60
    test_hors_plage = not en_plage

    ram_db.init_db()
    os.environ["RAM_SANS_ELECTION"] = "1"     # une seule instance en ligne
    manquants = ram_config.secrets_manquants()
    if manquants and not cfg.dry_run:
        log("WARN", "secrets manquants", manquants=manquants)
    vision_ok = False
    if cfg.val("vision.actif", True):
        try:
            vision_ok = bool(ram_vision.provider_par_defaut(cfg).disponible())
        except Exception:
            vision_ok = False

    log("INFO", "session continue", duree_min=round(duree_s / 60),
        plage=plage_horaire(cfg), sorties=[x.nom for x in reseau.sorties()],
        test_hors_plage=test_hors_plage)
    ARRET.clear()
    ARRET_SCAN.clear()
    signal_recu = threading.Event()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: signal_recu.set())
    except ValueError:
        pass                                   # hors du fil principal (tests)

    sources = cibles_scan_continu(cfg)
    autres = [("notification", worker_notification, (cfg,))]
    if vision_ok:
        autres.append(("vision", worker_vision, (cfg,)))
    if cfg.val("telegram.actif", True) and not cfg.dry_run \
            and ram_config.secret("TELEGRAM_BOT_TOKEN"):
        # Boutons des messages Telegram : possibles maintenant que le bot est
        # présent en permanence (un passage toutes les 10 min ne le permettait pas).
        autres.append(("callbacks", lambda c: ram_telegram.boucle_callbacks(cfg=c), (cfg,)))
    fils_scan, fils_autres = [], []
    for groupe, liste in ((sources, fils_scan), (autres, fils_autres)):
        for nom, cible, args in groupe:
            t = threading.Thread(target=_superviser, args=(nom, cible, args),
                                 name=f"ram-{nom}", daemon=True)
            t.start()
            liste.append(t)

    fin = time.time() + duree_s
    dernier_entretien = time.time()
    entretien = None
    motif = "durée"
    while True:
        if signal_recu.is_set():
            motif = "signal"
            break
        if time.time() >= fin:
            break
        if not test_hors_plage and not dans_plage_horaire(cfg):
            motif = "plage"
            break
        if time.time() - dernier_entretien >= 60 and not (entretien and entretien.is_alive()):
            dernier_entretien = time.time()
            cfg = ram_config.get()

            def _entretien(cfg=cfg):
                for tache in (lambda: signe_de_vie(cfg), reseau.sauver, _checkpoint_base,
                              _ecrire_sante, lambda: maintenance_quotidienne(cfg)):
                    try:
                        tache()
                    except Exception as e:
                        log("WARN", "tâche périodique en échec", erreur=str(e))
            # Dans son propre fil : le calibrage quotidien dure plusieurs
            # minutes et ne doit pas rendre la session sourde à l'arrêt.
            entretien = threading.Thread(target=_entretien, name="ram-entretien", daemon=True)
            entretien.start()
        time.sleep(pas_s)

    if motif == "signal":
        # Annulation GitHub : ~10 s avant d'être tué. On sauve, sans attendre.
        ARRET_SCAN.set()
        ARRET.set()
    else:
        ARRET_SCAN.set()
        for t in fils_scan:                    # laisse finir la requête en cours
            t.join(timeout=45)
        limite = time.time() + float(cfg.val("ci.budget_notifications_s", 240)) / 4
        while time.time() < limite:            # laisse partir ce qui est en file
            with _verrou_notif:
                if not _file_notif:
                    break
            time.sleep(0.5)
        ARRET.set()
        _reveil_notif.set()
        for t in fils_autres:
            if "callbacks" not in t.name:
                t.join(timeout=10)
    _checkpoint_base()
    reseau.sauver()
    try:
        # Relu avec « analyse les annonces » : téléchargé depuis l'onglet
        # Actions (artefact « alertes-envoyees »).
        n = ram_composants.exporter_alertes("alertes-envoyees.json", jours=2)
        log("INFO", "alertes exportées", fichier="alertes-envoyees.json", alertes=n)
    except Exception as e:
        log("WARN", "export des alertes impossible", erreur=str(e))

    relais = motif == "durée" and not test_hors_plage and dans_plage_horaire(cfg)
    _publier_relais(relais)
    log("INFO", "session terminée", motif=motif, relais=relais,
        nouvelles=STATS["annonces_nouvelles"], notifiees=STATS["notifiees"],
        rejetees=STATS["rejetees"], erreurs=STATS["erreurs"])
    return 0


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
    "--ci": "un passage complet (scan + notifications) puis sortie — hébergement cron",
    "--continu": "session longue sans arrêt (hébergement en ligne, relais automatique)",
    "--export-alertes": "écrit alertes-envoyees.json (alertes des 2 derniers jours)",
    "--dashboard": "sert le dashboard web (défaut : port 8010)",
    "--diag": "explique pourquoi les annonces sont rejetées",
    "--replay": "rejoue les annonces archivées à travers le scoring courant",
    "--calibrer": "lance le job de calibrage des prix",
    "--etat": "état complet du système, en JSON",
    "--seed": "recharge la base de références",
    "--prix-composants": "prix de marché des composants : grille vs annonces observées",
    "--force": "démarre même si une autre instance tourne déjà",
    "--limite=N": "nombre d'annonces à traiter (--diag, --replay)",
    "--port=N": "port du dashboard",
    "--categorie=X": "limite --prix-composants à une catégorie (CPU, GPU, MOBO…)",
}


def _valider_options(args, silencieux=False):
    inconnues = [a for a in args
                 if a not in OPTIONS
                 and not a.startswith(("--limite=", "--port=", "--categorie="))]
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
    Le mécanisme de verrouillage diffère selon l'OS (fcntl n'existe pas sous
    Windows, msvcrt n'existe pas sous Linux/macOS) mais l'appelant n'a pas à
    s'en soucier : dans les deux cas, .close() libère le verrou.
    """
    chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ram_sniper.lock")
    try:
        fd = open(chemin, "w")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except OSError:
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

    if "--prix-composants" in args:
        ram_db.init_db()
        categorie = next((a.split("=", 1)[1].upper() for a in args
                          if a.startswith("--categorie=")), None)
        ram_composants.afficher_rapport(cfg, categorie)
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

    if "--export-alertes" in args:
        ram_db.init_db()
        n = ram_composants.exporter_alertes("alertes-envoyees.json", jours=2)
        print(f"{n} alerte(s) → alertes-envoyees.json")
        sys.exit(0)

    if "--continu" in args:
        sys.exit(executer_continu(cfg))

    if "--ci" in args:
        sys.exit(executer_ci(cfg))

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
    print(f"  🌐 sorties     {', '.join(x.nom for x in reseau.sorties())}"
          "  (bascule automatique si l'une est bloquée)")
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
