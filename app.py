"""
app.py — PC FLIP SNIPER (vraie app web locale Flask)
═══════════════════════════════════════════════════════════════════════
Lance un serveur sur http://localhost:8000 avec :
  - un thread de fond qui scanne Leboncoin + Vinted en continu
  - une vraie interface live (mises à jour AJAX, pas de rechargement)
  - onglet Deals + onglet Évaluateur avec graphiques d'évolution
  - barre de recherche, filtres, contrôle start/pause du scan

Lancement :
    venv/bin/python3 app.py
    → ouvre automatiquement http://localhost:8000

Options (variables d'env):
    SNIPER_PORT=8000     port du serveur
    SNIPER_NO_VINTED=1   désactive Vinted
    SNIPER_NO_LBC=1      désactive Leboncoin
    SNIPER_STEAL=1       n'alerte que sur les affaires en or
"""

import os
import itertools
import sys
import json
import time
import random
import re
import threading
import webbrowser
from datetime import datetime

from flask import Flask, jsonify, request, Response

import lbc
from market_db import CATEGORIES, MIN_PRICE
from price_history import PriceHistory
from scoring import full_report
from perf_db import GPU_PERF, CPU_PERF
from listing_filter import analyze as analyze_listing
from relevance import model_matches, build_token_cache
from price_resolver import PriceResolver
from countries import get_country_config, list_countries
from settings_store import load_settings, save_settings
import soldier_db
import confidence

try:
    from vinted_client import VintedClient
    _HAS_VINTED = True
except Exception as e:
    _HAS_VINTED = False
    _VINTED_ERR = str(e)

try:
    from ebay_client import EbayClient, EbayError
    _HAS_EBAY_MODULE = True
except Exception as e:
    _HAS_EBAY_MODULE = False
    _EBAY_ERR = str(e)

try:
    from facebook_client import FacebookClient, FacebookError
    _HAS_FACEBOOK_MODULE = True
except Exception as e:
    _HAS_FACEBOOK_MODULE = False
    _FACEBOOK_ERR = str(e)

# ─────────────────────── CONFIG ───────────────────────
PORT = int(os.environ.get("SNIPER_PORT", "8000"))
STEAL_ONLY = os.environ.get("SNIPER_STEAL") == "1"

CAT_INFO = lbc.Category.ELECTRONIQUE_ACCESSOIRES_INFORMATIQUE
PSU_MIN_WATTS = 550
# Le confidence_score anti-bourrage (0-100, voir confidence.py) doit réellement
# filtrer/déclasser l'affichage — pas juste être un chiffre affiché à côté d'un
# badge trompeur. En dessous du seuil de rejet: l'annonce n'est pas affichée du
# tout. Entre rejet et seuil "steal": affichée, mais jamais taguée "affaire en
# or" (une vraie affaire en or doit être une annonce fiable, pas juste un prix
# anormalement bas qui peut cacher un bourrage de mots-clés ou une arnaque).
CONFIDENCE_REJECT_THRESHOLD = 35
CONFIDENCE_STEAL_MIN = 55
SCAN_INTERVAL = 90
LIMIT_PER_QUERY = 12
DELAY_MIN, DELAY_MAX = 1.4, 2.8
VINTED_DELAY = (1.0, 2.2)
EBAY_DELAY = (0.3, 0.8)   # API officielle, pas de scraping -> pas besoin d'être aussi prudent

DEALS_LOG = "deals_found.json"
SEEN_FILE = "seen_ads.json"
ARCHIVE_FILE = "deals_archive.jsonl"   # archive PERMANENTE, jamais purgée (JSON Lines: 1 deal par ligne)

# ── Nettoyage du dashboard ACTIF (pas de l'archive!) ────────────────────
# Le dashboard reste volontairement curé — deux mécanismes combinés:
#   1. Une annonce trop vieille (probablement vendue) sort du dashboard actif
#      après DEAL_MAX_AGE_DAYS (courte, un bon deal se vend vite).
#   2. VÉRIFICATION ACTIVE (voir revalidate_deals) : le sniper revisite
#      périodiquement les annonces actives pour confirmer qu'elles existent
#      toujours (via l'ID natif de chaque plateforme quand possible), et
#      retire immédiatement celles confirmées vendues/supprimées — pas besoin
#      d'attendre l'expiration par ancienneté pour ça.
# Dans les deux cas, l'annonce reste pour toujours dans l'archive séparée.
DEAL_MAX_AGE_DAYS = 5      # deals affichés dans le dashboard "live"
DEAL_MAX_COUNT = 3000      # cap du dashboard actif (au-delà: meilleurs scores + plus récents)
SEEN_TTL_DAYS = 90         # mémoire anti-doublon plus longue, pas de souci d'espace
REVALIDATE_BATCH_SIZE = 25 # nb d'annonces revérifiées par cycle (étalé pour ne pas surcharger)
REVALIDATE_MIN_AGE_HOURS = 3  # ne revérifie pas une annonce trouvée il y a moins de 3h (inutile)

FUNCTIONAL_WORDS = ["fonctionne","fonctionnel","fonctionnelle","testé","teste","testée","parfait état","parfait etat","très bon état","tres bon etat","bon état","comme neuf","rien à signaler","ras","garantie","facture","sous garantie","impeccable","nickel","opérationnel","operationnel"]
BROKEN_WORDS = ["hs","h.s","ne fonctionne pas","ne marche pas","pour pièce","pour pieces","pour pièces","défectueux","defectueux","en panne","panne","cassé","casse","ne démarre pas","ne demarre pas","ne boot pas","à réparer","a reparer","vendu en l'état","vendu en l etat","ne s'allume pas","artefact","artefacts","écran cassé","ecran casse","bloqué","bloque","problème","probleme"]
# (l'ancien SCAM_WORDS séparé a été fusionné dans listing_filter.py, qui centralise
# désormais TOUS les signaux d'arnaque en un seul score cohérent — voir _assess_scam_risk)

# Détection livraison pour Leboncoin (pas de champ structuré fiable -> heuristique texte)
SHIPPING_WORDS = ["livraison possible", "envoi possible", "envoi colissimo", "colissimo",
                  "peut envoyer", "envoi colis", "livraison colissimo", "shipping",
                  "envoi postal", "je peux envoyer", "expédition possible", "mondial relay"]
PICKUP_ONLY_WORDS = ["main propre uniquement", "remise en main propre uniquement",
                     "pas d'envoi", "pas de livraison", "sur place uniquement",
                     "à venir chercher", "retrait uniquement", "no shipping"]


def detect_shipping_lbc(subject, description):
    """Retourne True/False/None (True=livraison probable, False=retrait seul probable,
    None=indéterminé) à partir du texte de l'annonce Leboncoin."""
    text = (subject + " " + description).lower()
    if any(w in text for w in PICKUP_ONLY_WORDS):
        return False
    if any(w in text for w in SHIPPING_WORDS):
        return True
    return None

# tokens de pertinence par (catégorie, modèle) — calculés une seule fois au démarrage
TOKEN_CACHE = build_token_cache(CATEGORIES)

# ─────────────────────── ÉTAT PARTAGÉ ───────────────────────
STATE = {
    "running": True,
    "cycle": 0,
    "scanning": False,
    "current_cat": "",
    "current_model": "",
    "progress": 0.0,
    "last_cycle_new": 0,
    "started_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
    "last_update": "",
    "country": {},
    "lang": "fr",
    "sources_active": {},
    "settings_changed": False,
}
LOCK = threading.Lock()
RESOLVER = None  # instance de PriceResolver, créée par scan_worker au démarrage


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def parse_date_to_ts(value):
    """
    Convertit une date de publication (fournie par Leboncoin/Vinted/eBay dans
    des formats différents) en timestamp Unix. Retourne None si impossible à
    parser — dans ce cas on retombe sur l'heure de scan (voir make_deal).
    Accepte: chaîne ISO 8601 ("2026-07-11T12:25:52.000Z"), timestamp Unix
    (int/float ou chaîne numérique).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value.isdigit():
            return float(value)
        try:
            v = value.replace("Z", "+00:00")
            return datetime.fromisoformat(v).timestamp()
        except Exception:
            return None
    return None


def extract_watts(text):
    m = re.findall(r'(\d{3,4})\s*w(?:att)?s?\b', text.lower())
    w = [int(x) for x in m if 200 <= int(x) <= 1600]
    return max(w) if w else None

def assess(subject, description, price, ref):
    text = (subject + " " + description).lower()
    broken = any(w in text for w in BROKEN_WORDS)
    functional = any(w in text for w in FUNCTIONAL_WORDS)
    tier = None
    if price > 0 and not broken:
        if price <= ref["steal"]:
            tier = "steal"
        elif price <= ref["good"]:
            tier = "good"
    margin = ref["fair"] - price if price > 0 else 0
    return tier, functional, margin

def make_deal(cat, model, ref, ad, tier, functional, margin, source, listing=None):
    report = full_report(cat, model, ad["price"], ref, functional)

    # Liste de flags unifiée et traduisible: chaque entrée a une clé i18n +
    # ses paramètres, ET le texte français prêt à l'emploi (fr) pour l'archive
    # /les outils MCP qui n'ont pas besoin de traduction. listing_filter.py
    # centralise maintenant TOUS les signaux (état + arnaque) en une seule
    # analyse cohérente, donc un seul flag suffit ici.
    flags = []
    if listing:
        flags.append({"key": listing["reason_key"], "params": listing["reason_params"],
                      "fr": listing["reason"]})

    scan_ts = time.time()
    # "found_at"/tri "plus récent": utilise la VRAIE date de publication de
    # l'annonce quand la source la fournit (Leboncoin, Vinted, eBay — confirmé
    # fonctionnel), sinon on retombe sur l'heure de scan avec un indicateur
    # clair pour ne jamais laisser croire que c'est la date réelle.
    posted_ts = ad.get("posted_ts")
    date_is_real = posted_ts is not None
    display_ts = posted_ts if date_is_real else scan_ts

    # Score de confiance anti-bourrage (SOLDIER, niveaux 1-3, distinct du
    # scam_score de listing_filter.py) — voir confidence.py
    tokens_for_conf = TOKEN_CACHE.get((cat, model), {}).get("require", [])
    flat_tokens = [t for t in tokens_for_conf if isinstance(t, str)]
    conf_result = confidence.assess(ad["subject"], ad.get("description", ""), ad["price"],
                                    ref["fair"], model, model_tokens_flat=flat_tokens)

    # Niveau 4 (vision, optionnel/payant): déclenché seulement si activé dans
    # les réglages, sous le plafond de budget mensuel, et sur les candidats
    # ayant déjà passé les niveaux 1-3 — dégrade proprement sinon (voir
    # confidence.maybe_run_vision_check, jamais de crash sur erreur API/quota).
    try:
        v_penalty, v_reason = confidence.maybe_run_vision_check(
            conf_result["score"], ad.get("image", ""), model)
        if v_reason:
            conf_result["score"] = max(0, conf_result["score"] - v_penalty)
            conf_result["reasons"].append(v_reason)
    except Exception as e:
        print(f"      Vérification vision ignorée (erreur non bloquante): {e}")

    # Le confidence_score (anti-bourrage) doit réellement filtrer/déclasser
    # l'affichage, pas juste être un chiffre affiché à côté d'un "AFFAIRE EN OR"
    # trompeur — c'est le problème concret observé: un prix à 10% du marché
    # + un titre bourré de mots-clés ressortait quand même en "steal" en vert.
    if conf_result["score"] < CONFIDENCE_REJECT_THRESHOLD:
        return None  # trop suspect pour être affiché du tout
    if tier == "steal" and conf_result["score"] < CONFIDENCE_STEAL_MIN:
        tier = "good"  # une "affaire en or" doit être une annonce fiable, pas juste un prix bas

    d = {"category": cat, "model": model, "subject": ad["subject"], "price": ad["price"],
         "fair": ref["fair"], "good": ref["good"], "steal": ref["steal"], "margin": margin,
         "tier": tier, "functional": functional, "flags": flags, "url": ad["url"],
         "image": ad.get("image",""), "location": ad.get("location",""), "source": source,
         "ships": ad.get("ships"),
         "found_at": datetime.fromtimestamp(display_ts).strftime("%d/%m %H:%M"),
         "date_is_real": date_is_real,
         "posted_ts": display_ts,     # pour le tri "plus récent" (date réelle si dispo)
         "ts": scan_ts,               # heure de SCAN — sert uniquement au nettoyage/archivage
         "confidence_score": conf_result["score"],
         "confidence_reasons": conf_result["reasons"],
         "report": report}
    if listing:
        d["confidence"] = listing["confidence"]
        d["condition"] = listing["condition"]
        d["scam_risk"] = listing.get("scam_risk", 0)

    # Persiste dans la base SQLite unifiée SOLDIER (en plus du dict "deal"
    # utilisé par le dashboard existant) — c'est ce qui alimente le pipeline
    # achats/builds/ventes sans aucune ressaisie manuelle.
    try:
        soldier_db.upsert_listing(d, confidence=conf_result)
    except Exception as e:
        print(f"      ⚠️ Échec écriture SOLDIER DB: {e}")

    return d


# ─────────────────────── SCRAPERS ───────────────────────
def check_availability_lbc(client, url):
    """
    Vérifie si une annonce Leboncoin existe encore, via l'ID extrait de l'URL
    et l'API officielle de la lib lbc (get_ad). Retourne True/False/None
    (indéterminé — ex: erreur réseau/rate-limit, ne supprime rien dans ce cas).
    """
    if not client or not url:
        return None
    m = re.search(r'/(\d{6,})\.htm', url) or re.search(r'/(\d{6,})(?:[/?]|$)', url)
    if not m:
        return None
    ad_id = m.group(1)
    try:
        ad = client.get_ad(ad_id)
        status = (getattr(ad, "status", "") or "").lower()
        if any(bad in status for bad in ("expir", "delet", "remov", "refus", "inactiv", "closed")):
            return False
        return True
    except lbc.exceptions.NotFoundError:
        return False  # 404/410 précis: l'annonce a bien disparu
    except Exception:
        return None   # erreur réseau/rate-limit/autre: indéterminé, ne pas supprimer


def scan_lbc(client, cat, model, ref, seen, observed, dup_tracker=None):
    deals = []
    is_psu = (cat == "PSU")
    floor = MIN_PRICE.get(cat, 5)
    _tc = TOKEN_CACHE.get((cat, model), {"require": [], "exclude": []})
    for q in ref["queries"]:
        try:
            res = client.search(text=q, page=1, limit=LIMIT_PER_QUERY, sort=lbc.Sort.NEWEST,
                                 ad_type=lbc.AdType.OFFER, category=CAT_INFO,
                                 price=[floor, max(ref["fair"]*2, 50)])
            for ad in res.ads:
                url = ad.url or ""
                subject = ad.subject or ""
                desc = (ad.body or "")[:400] if hasattr(ad, "body") else ""
                price = ad.price if ad.price is not None else 0
                # pertinence: le titre/description doit VRAIMENT correspondre au modèle
                # (empêche un GTX 1060, un laptop, une alim... de s'étiqueter "RTX 5090")
                if not model_matches(subject, desc, _tc["require"], exclude=_tc["exclude"]):
                    continue
                if price > 0 and price < floor:
                    continue  # anti-bruit: sous le prix plancher = quasi toujours carton/pièce isolée
                if is_psu:
                    w = extract_watts(subject+" "+desc)
                    if w is not None and w < PSU_MIN_WATTS:
                        continue
                # filtre pertinence d'état (exclut carton/câble/waterblock/HS)
                listing = analyze_listing(subject, desc, cat, price=price, fair=ref["fair"], duplicate_tracker=dup_tracker, duplicate_key=(cat,model))
                if price > 0 and listing["keep"]:
                    observed.append(price)
                key = "lbc:"+url
                if not url or key in seen:
                    continue
                if not listing["keep"]:
                    continue
                tier, func, margin = assess(subject, desc, price, ref)
                if tier:
                    img = ""
                    if ad.images:
                        im = ad.images[0]; img = im.url if hasattr(im,"url") else str(im)
                    loc = ""
                    if ad.location:
                        parts=[]
                        if getattr(ad.location,"city",None): parts.append(ad.location.city)
                        if getattr(ad.location,"zipcode",None): parts.append(str(ad.location.zipcode))
                        loc=", ".join(parts)
                    norm={"subject":subject,"price":price,"url":url,"image":img,"location":loc,
                          "ships":detect_shipping_lbc(subject,desc),
                          "posted_ts":parse_date_to_ts(getattr(ad,"first_publication_date",None))}
                    d = make_deal(cat,model,ref,norm,tier,func,margin,"leboncoin",listing=listing)
                    if d:
                        deals.append(d)
                    seen[key]=time.time()
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        except lbc.exceptions.DatadomeError:
            time.sleep(15)
        except Exception:
            time.sleep(2)
    return deals

def scan_vinted(vc, cat, model, ref, seen, observed, dup_tracker=None):
    deals = []
    is_psu = (cat == "PSU")
    floor = MIN_PRICE.get(cat, 5)
    _tc = TOKEN_CACHE.get((cat, model), {"require": [], "exclude": []})
    for q in ref["queries"]:
        try:
            items = vc.search(search_text=q, price_to=max(ref["fair"], ref["good"]))
            for ad in items:
                url=ad["url"]; subject=ad["subject"]; desc=ad.get("description",""); price=ad["price"]
                if not model_matches(subject, desc, _tc["require"], exclude=_tc["exclude"]):
                    continue
                if price > 0 and price < floor:
                    continue  # anti-bruit: sous le prix plancher
                if is_psu:
                    w=extract_watts(subject+" "+desc)
                    if w is not None and w<PSU_MIN_WATTS: continue
                listing = analyze_listing(subject, desc, cat, price=price, fair=ref["fair"], duplicate_tracker=dup_tracker, duplicate_key=(cat,model))
                if price>0 and listing["keep"]:
                    observed.append(price)
                key="vinted:"+url
                if not url or key in seen: continue
                if not listing["keep"]:
                    continue
                tier,func,margin=assess(subject,desc,price,ref)
                if tier:
                    d = make_deal(cat,model,ref,ad,tier,func,margin,"vinted",listing=listing)
                    if d:
                        deals.append(d)
                    seen[key]=time.time()
            time.sleep(random.uniform(*VINTED_DELAY))
        except Exception as e:
            # IMPORTANT: on affiche vraiment l'erreur (avant: avalée en silence,
            # ce qui rendait "zéro résultat Vinted" impossible à diagnostiquer)
            print(f"      ✗ Vinted erreur sur '{q}': {e}")
            time.sleep(2)
    return deals


def scan_ebay(ec, cat, model, ref, seen, observed, dup_tracker=None):
    deals = []
    is_psu = (cat == "PSU")
    floor = MIN_PRICE.get(cat, 5)
    _tc = TOKEN_CACHE.get((cat, model), {"require": [], "exclude": []})
    for q in ref["queries"]:
        try:
            items = ec.search(q, price_to=max(ref["fair"], ref["good"]), price_from=floor)
            for ad in items:
                url=ad["url"]; subject=ad["subject"]; desc=ad.get("description",""); price=ad["price"]
                if not model_matches(subject, desc, _tc["require"], exclude=_tc["exclude"]):
                    continue
                if price > 0 and price < floor:
                    continue
                if is_psu:
                    w=extract_watts(subject+" "+desc)
                    if w is not None and w<PSU_MIN_WATTS: continue
                listing = analyze_listing(subject, desc, cat, price=price, fair=ref["fair"], duplicate_tracker=dup_tracker, duplicate_key=(cat,model))
                if price>0 and listing["keep"]:
                    observed.append(price)
                key="ebay:"+url
                if not url or key in seen: continue
                if not listing["keep"]:
                    continue
                tier,func,margin=assess(subject,desc,price,ref)
                if tier:
                    d = make_deal(cat,model,ref,ad,tier,func,margin,"ebay",listing=listing)
                    if d:
                        deals.append(d)
                    seen[key]=time.time()
            time.sleep(random.uniform(*EBAY_DELAY))
        except Exception as e:
            print(f"      ✗ eBay erreur sur '{q}': {e}")
            time.sleep(1)
    return deals


def scan_facebook(fc, cat, model, ref, seen, observed, dup_tracker=None, breaker=None):
    """
    breaker: dict partagé sur tout le cycle {"consecutive_fails": int, "tripped": bool}.
    Facebook rate-limite beaucoup plus fort que les autres sources — si on
    encaisse plusieurs échecs "rate limit" d'affilée, ça ne sert à rien de
    continuer à insister sur les 300+ modèles restants du cycle: on coupe
    Facebook pour le RESTE de ce cycle (le breaker est "tripped"), et il
    redémarre automatiquement proprement au cycle suivant.
    """
    deals = []
    is_psu = (cat == "PSU")
    floor = MIN_PRICE.get(cat, 5)
    _tc = TOKEN_CACHE.get((cat, model), {"require": [], "exclude": []})

    # Une seule requête (la plus spécifique) au lieu de toutes les variantes de
    # ref["queries"] -> réduit ~de moitié le volume de requêtes vers Facebook,
    # qui est la source la plus susceptible d'être rate-limitée.
    queries = ref["queries"][:1]

    for q in queries:
        if breaker is not None and breaker.get("tripped"):
            break
        try:
            items = fc.search(q, price_to=max(ref["fair"], ref["good"]), price_from=floor)
            if breaker is not None:
                breaker["consecutive_fails"] = 0  # succès -> on réinitialise le compteur d'échecs
            for ad in items:
                url=ad["url"]; subject=ad["subject"]; desc=ad.get("description",""); price=ad["price"]
                if not model_matches(subject, desc, _tc["require"], exclude=_tc["exclude"]):
                    continue
                if price > 0 and price < floor:
                    continue
                if is_psu:
                    w=extract_watts(subject+" "+desc)
                    if w is not None and w<PSU_MIN_WATTS: continue
                listing = analyze_listing(subject, desc, cat, price=price, fair=ref["fair"], duplicate_tracker=dup_tracker, duplicate_key=(cat,model))
                if price>0 and listing["keep"]:
                    observed.append(price)
                key="facebook:"+url
                if not url or key in seen: continue
                if not listing["keep"]:
                    continue
                tier,func,margin=assess(subject,desc,price,ref)
                if tier:
                    d = make_deal(cat,model,ref,ad,tier,func,margin,"facebook",listing=listing)
                    if d:
                        deals.append(d)
                    seen[key]=time.time()
            time.sleep(random.uniform(2.5, 4.5))  # Facebook rate-limite plus fort que les autres
        except FacebookError as e:
            is_rate_limit = "rate limit" in str(e).lower()
            if breaker is not None:
                if is_rate_limit:
                    breaker["consecutive_fails"] += 1
                    if breaker["consecutive_fails"] >= 3:
                        breaker["tripped"] = True
                        print(f"      ⏸ Facebook: rate-limité 3 fois d'affilée — coupé pour le "
                              f"reste de ce cycle (reprendra normalement au prochain cycle)")
                else:
                    breaker["consecutive_fails"] = 0
            print(f"      ✗ Facebook erreur sur '{q}': {e}")
            time.sleep(5 if is_rate_limit else 3)
        except Exception as e:
            print(f"      ✗ Facebook erreur sur '{q}': {e}")
            time.sleep(3)
    return deals


# ─────────────────────── NETTOYAGE AUTOMATIQUE (anti-bloat) ───────────────────────
def prune_seen(seen):
    """Oublie les annonces vues il y a plus de SEEN_TTL_DAYS (le fichier ne grossit
    plus indéfiniment). Les valeurs anciennes non-numériques (ex: True) sont traitées
    comme expirées pour forcer leur purge après mise à jour du format."""
    cutoff = time.time() - SEEN_TTL_DAYS * 86400
    dead = [k for k, v in seen.items() if not isinstance(v, (int, float)) or v < cutoff]
    for k in dead:
        del seen[k]
    return len(dead)


def archive_deal(d):
    """Écrit ce deal dans l'archive PERMANENTE (jamais purgée). Un fichier
    JSON Lines: une ligne = un deal complet, facile à ajouter (append) sans
    jamais réécrire tout le fichier, même quand il devient énorme."""
    try:
        with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except Exception:
        pass


def prune_deals(all_deals):
    """Purge le dashboard ACTIF des deals trop vieux (probablement vendus) et
    cap le nombre affiché. Ne touche JAMAIS à l'archive permanente — les deals
    retirés d'ici restent consultables pour toujours dans ARCHIVE_FILE."""
    now = time.time()
    cutoff = now - DEAL_MAX_AGE_DAYS * 86400
    fresh = [d for d in all_deals if d.get("ts", now) >= cutoff]
    removed_age = len(all_deals) - len(fresh)

    removed_cap = 0
    if len(fresh) > DEAL_MAX_COUNT:
        # garde un mix: meilleurs scores globaux + plus récents
        fresh.sort(key=lambda d: (d.get("report", {}).get("global_score", 0), d.get("ts", 0)), reverse=True)
        removed_cap = len(fresh) - DEAL_MAX_COUNT
        fresh = fresh[:DEAL_MAX_COUNT]

    return fresh, removed_age, removed_cap


def cleanup_cycle(seen, all_deals):
    """Appelé après chaque cycle de scan: nettoyage du dashboard actif
    uniquement (l'archive n'est jamais touchée, voir archive_deal)."""
    n_seen = prune_seen(seen)
    all_deals[:], n_age, n_cap = prune_deals(all_deals)
    if n_seen or n_age or n_cap:
        print(f"      🧹 Dashboard actif nettoyé: -{n_seen} annonces oubliées (>{SEEN_TTL_DAYS}j) · "
              f"-{n_age} deals retirés du live (>{DEAL_MAX_AGE_DAYS}j, toujours dans l'archive) · "
              f"-{n_cap} en surplus (cap {DEAL_MAX_COUNT})")


def revalidate_deals(all_deals, client, vc, ec, fc):
    """
    Revisite un LOT d'annonces déjà trouvées pour confirmer qu'elles sont
    encore réellement en vente, et retire immédiatement celles confirmées
    vendues/supprimées — pas besoin d'attendre l'expiration par ancienneté.

    Étalé sur REVALIDATE_BATCH_SIZE annonces par cycle (les plus anciennes
    d'abord) pour ne pas surcharger les plateformes ni ralentir le scan.
    Une vérification "indéterminée" (erreur réseau, source non gérée) laisse
    l'annonce en place — on ne supprime que sur confirmation claire.
    """
    now = time.time()
    min_age = REVALIDATE_MIN_AGE_HOURS * 3600
    candidates = [d for d in all_deals if now - d.get("ts", now) >= min_age]
    if not candidates:
        return 0
    candidates.sort(key=lambda d: d.get("_last_checked", 0))
    batch = candidates[:REVALIDATE_BATCH_SIZE]

    removed_urls = set()
    for d in batch:
        d["_last_checked"] = now
        source = d.get("source")
        avail = None
        try:
            if source == "leboncoin" and client:
                avail = check_availability_lbc(client, d["url"])
            elif source == "vinted" and vc:
                avail = vc.check_availability(d["url"])
            elif source == "ebay" and ec:
                avail = ec.check_availability(d.get("item_id"))
            elif source == "facebook" and fc:
                avail = fc.check_availability(d["url"])
        except Exception:
            avail = None
        if avail is False:
            removed_urls.add(d["url"])

    if removed_urls:
        all_deals[:] = [d for d in all_deals if d["url"] not in removed_urls]
        print(f"      🔍 Revérification: {len(removed_urls)} annonce(s) confirmée(s) "
              f"vendue(s)/supprimée(s) retirée(s) du dashboard (sur {len(batch)} vérifiées)")
    return len(removed_urls)


# ─────────────────────── THREAD DE SCAN ───────────────────────
def build_clients(settings):
    """(Re)construit les clients réseau selon les paramètres actuels
    (pays + plateformes activées). Retourne (country_cfg, client_lbc, vc, ec, fc)."""
    country_cfg = get_country_config(settings.get("country"))
    sources = settings.get("sources", {})

    lbc_active = sources.get("lbc", True) and country_cfg.get("has_lbc", False)
    client = lbc.Client(max_retries=3) if lbc_active else None

    vinted_active = sources.get("vinted", True) and country_cfg.get("has_vinted", False) and _HAS_VINTED
    vc = VintedClient(country=country_cfg["code"]) if vinted_active else None

    ec = None
    if sources.get("ebay", True) and _HAS_EBAY_MODULE:
        try:
            ec = EbayClient(country=country_cfg["code"])
            print(f"   ✓ eBay activé (marketplace {ec.marketplace_id})")
        except EbayError as e:
            print(f"   ⚠️ eBay désactivé: {e}")
        except Exception as e:
            print(f"   ⚠️ eBay désactivé (erreur inattendue): {e}")

    fc = None
    if sources.get("facebook", True) and _HAS_FACEBOOK_MODULE:
        try:
            custom_location = (settings.get("location") or "").strip() or None
            radius_km = settings.get("radius_km") or 16
            fc = FacebookClient(country_code=country_cfg["code"], location_query=custom_location,
                                radius_km=radius_km)
            print(f"   ✓ Facebook Marketplace activé (autour de {fc.location_query})")
        except FacebookError as e:
            print(f"   ⚠️ Facebook Marketplace désactivé: {e}")
        except Exception as e:
            print(f"   ⚠️ Facebook Marketplace désactivé (erreur inattendue): {e}")

    return country_cfg, lbc_active, client, vc, ec, fc



def build_scan_order(categories):
    """Construit un ordre de scan qui MÉLANGE les catégories (round-robin) au lieu
    de les épuiser une par une dans l'ordre. Sans ça, GPU (84 modèles, la plus
    grosse catégorie) passe en premier et il faut 20+ minutes avant même de voir
    un seul CPU ou boîtier apparaître. Avec le mélange, on voit des résultats de
    TOUTES les catégories dès les premières minutes du cycle."""
    per_cat_lists = [
        [(cat_key, model_name, static_ref) for model_name, static_ref in cat["db"].items()]
        for cat_key, cat in categories.items()
    ]
    order = []
    for group in itertools.zip_longest(*per_cat_lists, fillvalue=None):
        for item in group:
            if item is not None:
                order.append(item)
    return order


def scan_worker():
    # ── état persistant, chargé UNE SEULE FOIS (ne dépend pas du pays/plateformes) ──
    seen = load_json(SEEN_FILE, {})
    all_deals = load_json(DEALS_LOG, [])
    history = PriceHistory()

    with LOCK:
        STATE["deals"] = all_deals

    resolver = PriceResolver(price_history=history)
    global RESOLVER
    RESOLVER = resolver

    total = sum(len(c["db"]) for c in CATEGORIES.values())

    # ── boucle externe: se relance à chaque changement de pays/plateformes ──
    while True:
        settings = load_settings()
        country_cfg, lbc_active, client, vc, ec, fc = build_clients(settings)
        with LOCK:
            STATE["country"] = country_cfg
            STATE["lang"] = settings.get("lang", "fr")
            STATE["sources_active"] = {"lbc": lbc_active, "vinted": vc is not None,
                                        "ebay": ec is not None, "facebook": fc is not None}
            STATE["settings_changed"] = False
        print(f"   🌍 Pays actif: {country_cfg['label']} ({country_cfg['code']}) — "
              f"LBC:{'oui' if lbc_active else 'non'} Vinted:{'oui' if vc else 'non'} "
              f"eBay:{'oui' if ec else 'non'} Facebook:{'oui' if fc else 'non'}")

        # ── boucle interne: cycles de scan normaux avec cette config ──
        while not STATE.get("settings_changed"):
            if not STATE["running"]:
                time.sleep(2); continue

            STATE["cycle"] += 1
            STATE["scanning"] = True
            new_this_cycle = 0
            done = 0
            resolver.reload_live()  # relit live_prices.json une fois par cycle (léger, à jour)
            scan_order = build_scan_order(CATEGORIES)  # mélangé: GPU, CPU, boîtier, GPU, CPU...
            dup_tracker = {}  # remis à zéro à chaque cycle: détecte le spam de bots
            fb_breaker = {"consecutive_fails": 0, "tripped": False}  # coupe-circuit rate-limit Facebook

            for cat_key, model_name, static_ref in scan_order:
                if not STATE["running"] or STATE.get("settings_changed"):
                    break
                done += 1
                with LOCK:
                    STATE["current_cat"] = CATEGORIES[cat_key]["label"]
                    STATE["current_model"] = model_name
                    STATE["progress"] = round(done/total*100, 1)
                ref = resolver.resolve(cat_key, model_name, static_ref)
                observed = []
                found = []
                if lbc_active and client:
                    found += scan_lbc(client, cat_key, model_name, ref, seen, observed, dup_tracker=dup_tracker)
                if vc:
                    found += scan_vinted(vc, cat_key, model_name, ref, seen, observed, dup_tracker=dup_tracker)
                if ec:
                    found += scan_ebay(ec, cat_key, model_name, ref, seen, observed, dup_tracker=dup_tracker)
                if fc and not fb_breaker["tripped"]:
                    fb_result = scan_facebook(fc, cat_key, model_name, ref, seen, observed,
                                              dup_tracker=dup_tracker, breaker=fb_breaker)
                    found += fb_result
                history.record(cat_key, model_name, observed)
                if STEAL_ONLY:
                    found = [d for d in found if d["tier"]=="steal"]
                if found:
                    with LOCK:
                        for d in found:
                            all_deals.append(d)
                            archive_deal(d)   # archive permanente, jamais supprimée
                        new_this_cycle += len(found)

            STATE["scanning"] = False
            STATE["last_cycle_new"] = new_this_cycle
            STATE["last_update"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

            cleanup_cycle(seen, all_deals)
            revalidate_deals(all_deals, client if lbc_active else None, vc, ec, fc)
            with LOCK:
                STATE["deals"] = all_deals

            save_json(SEEN_FILE, seen)
            save_json(DEALS_LOG, all_deals)
            history.save()
            with LOCK:
                STATE["_history"] = history.export_compact()

            for _ in range(SCAN_INTERVAL):
                if not STATE["running"] or STATE.get("settings_changed"):
                    break
                time.sleep(1)

        print("   🔁 Paramètres modifiés — redémarrage du scan avec la nouvelle config…")


# ─────────────────────── FLASK APP ───────────────────────
app = Flask(__name__)


class ValidationError(Exception):
    """Payload invalide côté client (champ manquant, valeur incohérente) —
    toujours renvoyée en 400 avec un message clair, jamais une stacktrace."""
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def positive_number(value, field, allow_zero=True):
    if value is None:
        raise ValidationError(f"{field} requis")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} doit être un nombre")
    if allow_zero and value < 0:
        raise ValidationError(f"{field} ne peut pas être négatif")
    if not allow_zero and value <= 0:
        raise ValidationError(f"{field} doit être strictement positif")
    return value


@app.errorhandler(ValidationError)
def handle_validation_error(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(404)
def handle_not_found(e):
    return jsonify({"error": "route introuvable"}), 404


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description or "erreur"}), e.code
    print(f"      Erreur non gérée sur {request.path}: {e}")
    return jsonify({"error": "erreur interne, réessaie dans un instant"}), 500


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

@app.route("/api/state")
def api_state():
    with LOCK:
        return jsonify({k: v for k, v in STATE.items() if not k.startswith("_") and k != "deals"})

@app.route("/api/deals")
def api_deals():
    with LOCK:
        deals = list(STATE.get("deals", []))
    deals.sort(key=lambda d: (0 if d["tier"]=="steal" else 1, -d["margin"]))
    return jsonify(deals[:400])

def resolve_ref(cat, model):
    """Trouve la référence statique puis la fait passer par le résolveur
    (marché réel > PCPartPicker > statique) si le résolveur est prêt."""
    static_ref, cat_label = None, ""
    for ck, c in CATEGORIES.items():
        if ck == cat and model in c["db"]:
            static_ref = c["db"][model]; cat_label = c["label"]; break
    if static_ref is None:
        return None, None
    if RESOLVER is not None:
        ref = RESOLVER.resolve(cat, model, static_ref)
    else:
        ref = {**static_ref, "source": "estimation", "note": "résolveur pas encore initialisé"}
    return ref, cat_label


@app.route("/api/catalog")
def api_catalog():
    cat = []
    for ck, c in CATEGORIES.items():
        for model, static_ref in c["db"].items():
            ref, _ = resolve_ref(ck, model)
            cat.append({"cat":ck,"catLabel":c["label"],"color":c["color"],"model":model,
                        "fair":ref["fair"],"good":ref["good"],"steal":ref["steal"],
                        "source":ref.get("source","estimation")})
    return jsonify({"catalog": cat,
                    "categories": {k:{"label":v["label"],"color":v["color"]} for k,v in CATEGORIES.items()}})

@app.route("/api/history")
def api_history():
    with LOCK:
        return jsonify(STATE.get("_history", {}))

@app.route("/api/evaluate")
def api_evaluate():
    """Rapport perf/prix pour un modèle à un prix donné (ou au prix 'fair')."""
    cat = request.args.get("cat")
    model = request.args.get("model")
    ref, _ = resolve_ref(cat, model)
    if not ref:
        return jsonify({"error": "modèle introuvable"}), 404
    try:
        price = int(request.args.get("price", ref["fair"]))
    except ValueError:
        price = ref["fair"]
    rep = full_report(cat, model, price, ref, True)
    return jsonify({"model": model, "cat": cat, "price": price, "ref": ref, "report": rep})


@app.route("/api/model_detail")
def api_model_detail():
    """Fiche complète d'un modèle: référence, rapport, historique de prix
    (série journalière) et les annonces actuellement connues qui correspondent
    — c'est cette route qui alimente la page détail (plus de redirection directe)."""
    cat = request.args.get("cat")
    model = request.args.get("model")
    ref, cat_label = resolve_ref(cat, model)
    if not ref:
        return jsonify({"error": "modèle introuvable"}), 404

    report = full_report(cat, model, ref["fair"], ref, True)

    ph = PriceHistory()
    series = ph.series(cat, model)

    with LOCK:
        deals = list(STATE.get("deals", []))
    listings = [d for d in deals if d["category"] == cat and d["model"] == model]
    listings.sort(key=lambda d: d.get("posted_ts", d.get("ts", 0)), reverse=True)
    listings = [{"subject": d["subject"], "price": d["price"], "url": d["url"],
                 "source": d["source"], "location": d.get("location", ""),
                 "confidence": d.get("confidence"), "found_at": d.get("found_at", ""),
                 "date_is_real": d.get("date_is_real", False)}
                for d in listings[:30]]

    return jsonify({"model": model, "cat": cat, "catLabel": cat_label, "ref": ref,
                    "report": report, "history": series, "listings": listings})


@app.route("/api/control", methods=["POST"])
def api_control():
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "pause":
        STATE["running"] = False
    elif action == "resume":
        STATE["running"] = True
    return jsonify({"running": STATE["running"]})


# ═══════════════════════════════════════════════════════════════════
#  ROUTES SOLDIER — pipeline achats/builds/ventes
# ═══════════════════════════════════════════════════════════════════

@app.route("/api/soldier/listings")
def api_soldier_listings():
    status = request.args.get("status")
    try:
        min_conf = int(request.args.get("min_confidence", 0) or 0)
    except ValueError:
        raise ValidationError("min_confidence doit être un entier")
    return jsonify(soldier_db.list_listings(status=status, min_confidence=min_conf))


@app.route("/api/soldier/pipeline/send", methods=["POST"])
def api_soldier_send_to_pipeline():
    """Le handoff en un clic: un deal validé devient un achat, sans ressaisie."""
    data = request.json or {}
    require(data.get("listing_id") is not None, "listing_id requis")
    purchase_id = soldier_db.send_listing_to_pipeline(data["listing_id"])
    if purchase_id is None:
        return jsonify({"error": "listing introuvable"}), 404
    return jsonify({"ok": True, "purchase_id": purchase_id})


@app.route("/api/soldier/purchases", methods=["GET", "POST"])
def api_soldier_purchases():
    if request.method == "GET":
        return jsonify(soldier_db.list_purchases(status=request.args.get("status")))
    data = request.json or {}
    require(isinstance(data, dict), "payload JSON invalide")
    require(bool((data.get("model") or "").strip()), "model requis")
    positive_number(data.get("buy_price"), "buy_price", allow_zero=False)
    if "shipping_cost" in data:
        positive_number(data["shipping_cost"], "shipping_cost")
    if "buyer_protection_fee" in data:
        positive_number(data["buyer_protection_fee"], "buyer_protection_fee")
    purchase_id = soldier_db.create_purchase(data)
    return jsonify({"ok": True, "id": purchase_id})


@app.route("/api/soldier/purchases/<int:purchase_id>", methods=["PATCH", "DELETE"])
def api_soldier_purchase_detail(purchase_id):
    if request.method == "DELETE":
        soldier_db.delete_purchase(purchase_id)
        return jsonify({"ok": True, "trashed": True})
    data = request.json or {}
    if "buy_price" in data:
        positive_number(data["buy_price"], "buy_price", allow_zero=False)
    if "shipping_cost" in data:
        positive_number(data["shipping_cost"], "shipping_cost")
    if "buyer_protection_fee" in data:
        positive_number(data["buyer_protection_fee"], "buyer_protection_fee")
    soldier_db.update_purchase(purchase_id, data)
    return jsonify({"ok": True})


@app.route("/api/soldier/purchases/<int:purchase_id>/restore", methods=["POST"])
def api_soldier_purchase_restore(purchase_id):
    soldier_db.restore_purchase(purchase_id)
    return jsonify({"ok": True})


@app.route("/api/soldier/purchases/<int:purchase_id>/purge", methods=["DELETE"])
def api_soldier_purchase_purge(purchase_id):
    soldier_db.purge_purchase(purchase_id)
    return jsonify({"ok": True})


@app.route("/api/soldier/builds", methods=["GET", "POST"])
def api_soldier_builds():
    if request.method == "GET":
        return jsonify(soldier_db.list_builds())
    data = request.json or {}
    require(bool((data.get("name") or "").strip()), "name requis")
    if "extra_costs" in data:
        positive_number(data["extra_costs"], "extra_costs")
    if data.get("target_price") is not None:
        positive_number(data["target_price"], "target_price", allow_zero=False)
    build_id = soldier_db.create_build(data["name"], data.get("extra_costs", 0), data.get("target_price"),
                                        data.get("tags", ""), data.get("notes"))
    return jsonify({"ok": True, "id": build_id})


@app.route("/api/soldier/builds/<int:build_id>", methods=["DELETE"])
def api_soldier_build_delete(build_id):
    soldier_db.delete_build(build_id)
    return jsonify({"ok": True, "trashed": True})


@app.route("/api/soldier/builds/<int:build_id>/restore", methods=["POST"])
def api_soldier_build_restore(build_id):
    soldier_db.restore_build(build_id)
    return jsonify({"ok": True})


@app.route("/api/soldier/builds/<int:build_id>/purge", methods=["DELETE"])
def api_soldier_build_purge(build_id):
    soldier_db.purge_build(build_id)
    return jsonify({"ok": True})


@app.route("/api/soldier/trash")
def api_soldier_trash():
    return jsonify({"purchases": soldier_db.list_trash_purchases(), "builds": soldier_db.list_trash_builds()})


@app.route("/api/soldier/builds/<int:build_id>/attach", methods=["POST"])
def api_soldier_build_attach(build_id):
    data = request.json or {}
    require(data.get("purchase_id") is not None, "purchase_id requis")
    soldier_db.attach_purchase_to_build(data["purchase_id"], build_id)
    return jsonify({"ok": True})


@app.route("/api/soldier/purchases/<int:purchase_id>/detach", methods=["POST"])
def api_soldier_purchase_detach(purchase_id):
    soldier_db.detach_purchase_from_build(purchase_id)
    return jsonify({"ok": True})


@app.route("/api/soldier/purchases/<int:purchase_id>/suggest_price")
def api_soldier_suggest_price(purchase_id):
    """Suggestion de prix de vente à partir du prix marché du listing d'origine
    (si connu), sinon une marge par défaut raisonnable sur le prix d'achat."""
    purchases = soldier_db.list_purchases(include_deleted=True)
    p = next((x for x in purchases if x["id"] == purchase_id), None)
    if not p:
        return jsonify({"error": "achat introuvable"}), 404
    suggestion = None
    if p.get("listing_id"):
        listings = soldier_db.list_listings(limit=5000)
        listing = next((l for l in listings if l["id"] == p["listing_id"]), None)
        if listing and listing.get("market_price"):
            suggestion = listing["market_price"]
    total_cost = (p.get("buy_price") or 0) + (p.get("shipping_cost") or 0) + (p.get("buyer_protection_fee") or 0)
    if not suggestion:
        suggestion = round(total_cost * 1.25, 2)
    return jsonify({"suggested_price": suggestion, "total_cost": round(total_cost, 2)})


@app.route("/api/soldier/sales", methods=["GET", "POST"])
def api_soldier_sales():
    if request.method == "GET":
        return jsonify(soldier_db.list_sales())
    data = request.json or {}
    positive_number(data.get("sale_price"), "sale_price", allow_zero=False)
    require(bool(data.get("purchase_id")) != bool(data.get("build_id")),
            "exactement un de purchase_id ou build_id requis")
    if "fees" in data:
        positive_number(data["fees"], "fees")
    sale_id = soldier_db.create_sale(data)
    return jsonify({"ok": True, "id": sale_id})


@app.route("/api/soldier/dashboard")
def api_soldier_dashboard():
    return jsonify(soldier_db.dashboard_kpis())


@app.route("/api/soldier/analytics")
def api_soldier_analytics():
    return jsonify(soldier_db.analytics_summary())


def _csv_safe(value):
    """Neutralise l'injection de formule CSV (une cellule commençant par
    =, +, -, @, tab ou CR peut s'exécuter comme une formule à l'ouverture
    dans Excel/Numbers/LibreOffice) — préfixe d'une apostrophe si besoin.
    Champs concernés: model/category/source/tags/notes/platform, tous
    éditables librement par l'utilisateur ou copiés depuis une annonce."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@app.route("/api/soldier/export/purchases.csv")
def api_soldier_export_purchases():
    import csv, io
    purchases = soldier_db.list_purchases()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "model", "category", "buy_price", "shipping_cost", "buyer_protection_fee",
                "source", "status", "purchase_date", "days_in_stock", "tags", "notes"])
    for p in purchases:
        date_str = datetime.fromtimestamp(p["purchase_date"]).strftime("%Y-%m-%d") if p.get("purchase_date") else ""
        w.writerow([p["id"], _csv_safe(p["model"]), _csv_safe(p.get("category", "")), p["buy_price"],
                    p.get("shipping_cost", 0), p.get("buyer_protection_fee", 0), _csv_safe(p.get("source", "")),
                    p["status"], date_str, p.get("days_in_stock", ""), _csv_safe(p.get("tags", "")),
                    _csv_safe(p.get("notes", ""))])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=achats_soldier.csv"})


@app.route("/api/soldier/export/sales.csv")
def api_soldier_export_sales():
    import csv, io
    sales = soldier_db.list_sales()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "purchase_id", "build_id", "sale_price", "platform", "fees", "net_margin", "sale_date"])
    for s in sales:
        date_str = datetime.fromtimestamp(s["sale_date"]).strftime("%Y-%m-%d") if s.get("sale_date") else ""
        w.writerow([s["id"], s.get("purchase_id", ""), s.get("build_id", ""), s["sale_price"],
                    _csv_safe(s.get("platform", "")), s.get("fees", 0), s.get("net_margin", 0), date_str])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ventes_soldier.csv"})


@app.route("/api/soldier/import", methods=["POST"])
def api_soldier_import():
    """Route d'import: bascule les données de l'ancien SOLDER (export JSON
    {"purchases":[...], "builds":[...]}) vers la nouvelle base SQLite."""
    result = soldier_db.migrate_from_solder_export(request.json or {})
    return jsonify({"ok": True, **result})


# ═══════════════════════════════════════════════════════════════════
#  ONBOARDING
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/onboarding/status")
def api_onboarding_status():
    return jsonify({
        "onboarded": soldier_db.is_onboarded(),
        "preferences": soldier_db.get_kv("preferences", {}),
        "vision": confidence.vision_budget_status(),
    })


@app.route("/api/onboarding/complete", methods=["POST"])
def api_onboarding_complete():
    data = request.json or {}
    prefs = data.get("preferences", {})
    soldier_db.set_kv("preferences", prefs)
    vision = data.get("vision", {})
    soldier_db.set_kv("vision_enabled", bool(vision.get("enabled", False)))
    budget = vision.get("budget_eur_monthly", 0)
    positive_number(budget, "vision.budget_eur_monthly")
    soldier_db.set_kv("vision_budget_eur_monthly", float(budget))

    starter = data.get("starter_data")  # "demo" | "import" | None
    result = {}
    if starter == "demo":
        result = soldier_db.load_demo_data()
    elif starter == "import" and data.get("import_payload"):
        result = soldier_db.migrate_from_solder_export(data["import_payload"])

    soldier_db.set_onboarded(True)
    return jsonify({"ok": True, **result})


@app.route("/api/onboarding/reset", methods=["POST"])
def api_onboarding_reset():
    """Relance l'onboarding depuis les Réglages — ne touche à aucune donnée,
    juste au flag d'affichage."""
    soldier_db.set_onboarded(False)
    return jsonify({"ok": True})


@app.route("/api/countries")
def api_countries():
    """Liste tous les pays disponibles pour peupler le sélecteur du dashboard."""
    return jsonify({"countries": list_countries()})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """
    GET: renvoie les paramètres actuels (pays, langue, plateformes activées)
    POST: les met à jour, persiste dans sniper_settings.json, et signale au
    thread de scan de se relancer avec la nouvelle config (sans redémarrer
    tout le processus).
    """
    if request.method == "GET":
        s = load_settings()
        with LOCK:
            s["sources_active"] = dict(STATE.get("sources_active", {}))
            s["country_resolved"] = STATE.get("country", {})
        return jsonify(s)

    data = request.json or {}
    settings = load_settings()
    if "country" in data:
        settings["country"] = data["country"].upper()
    if "lang" in data:
        settings["lang"] = data["lang"]
    if "location" in data:
        settings["location"] = data["location"]
    if "radius_km" in data:
        settings["radius_km"] = data["radius_km"]
    if "sources" in data:
        settings["sources"] = {**settings.get("sources", {}), **data["sources"]}
    save_settings(settings)
    STATE["settings_changed"] = True
    with LOCK:
        STATE["lang"] = settings.get("lang", "fr")
    return jsonify({"ok": True, "settings": settings})


# ─────────────────────── SAUVEGARDE AUTOMATIQUE ───────────────────────
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
BACKUP_INTERVAL_S = 12 * 3600   # toutes les 12h
BACKUP_KEEP = 30                # ne garde que les 30 sauvegardes les plus récentes


def backup_db():
    """Copie horodatée de soldier.db dans backups/. Filet de sécurité simple:
    en cas de corruption ou de fausse manip, on peut toujours revenir à un
    snapshot récent. N'échoue jamais bruyamment (juste loggé)."""
    try:
        import shutil
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if not os.path.exists(soldier_db.DB_FILE):
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"soldier_{stamp}.db")
        shutil.copy2(soldier_db.DB_FILE, dest)
        backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("soldier_") and f.endswith(".db"))
        for old in backups[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except OSError:
                pass
    except Exception as e:
        print(f"      Sauvegarde soldier.db ignorée (erreur non bloquante): {e}")


def backup_worker():
    while True:
        backup_db()
        time.sleep(BACKUP_INTERVAL_S)


def main():
    soldier_db.init_db()  # crée les tables SOLDIER (listings/purchases/builds/sales) si absentes
    backup_db()  # snapshot immédiat au démarrage, avant toute écriture de la session
    threading.Thread(target=backup_worker, daemon=True).start()

    # charge historique existant dans STATE au démarrage
    h = PriceHistory()
    STATE["_history"] = h.export_compact()
    STATE["deals"] = load_json(DEALS_LOG, [])

    settings = load_settings()
    country_cfg = get_country_config(settings.get("country"))

    t = threading.Thread(target=scan_worker, daemon=True)
    t.start()

    url = f"http://localhost:{PORT}"
    print(f"\n🎯 PC FLIP SNIPER — app web lancée sur {url}")
    print(f"   Pays configuré: {country_cfg['label']} ({country_cfg['code']}) "
          f"— modifiable dans les paramètres du dashboard")
    print(f"   Le scan tourne en fond. Ouvre {url} (ouverture auto dans 1,5s)\n")

    def open_browser():
        time.sleep(1.5)
        try: webbrowser.open(url)
        except Exception: pass
    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)


# ─────────────────────── FRONT (single page, live AJAX) ───────────────────────
INDEX_HTML = r'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SOLDIER</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230a0a0b'/%3E%3Crect x='9' y='9' width='14' height='14' rx='3' fill='%233b82f6'/%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@400;600;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0a0a0b;--s1:#121214;--s2:#18181b;--s3:#1e1e22;--bd:#26262a;--tx:#e8e8ea;--mu:#8a8a92;--accent:#3b82f6;--gold:#eab308;--steal:#ef4444;--lbc:#8a8a92;--vinted:#8a8a92;--ebay:#8a8a92;--facebook:#8a8a92}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:"DM Sans",sans-serif;min-height:100vh}
header{background:linear-gradient(135deg,#0e1318,#141b22);border-bottom:1px solid var(--bd);padding:1.4rem 2rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;position:sticky;top:0;z-index:50}
.logo{font-family:"Space Mono",monospace;font-size:1.35rem;font-weight:700;color:var(--accent)}
.logo span{color:var(--steal)}
.logo small{display:block;font-size:.6rem;color:var(--mu);font-weight:400;margin-top:.25rem}
.stats{display:flex;gap:1.3rem;flex-wrap:wrap;align-items:center}
.stat{text-align:center}
.sv{font-family:"Space Mono",monospace;font-size:1.3rem;font-weight:700;color:var(--accent);line-height:1}
.sv.gold{color:var(--gold)}.sv.steal{color:var(--steal)}.sv.lbc{color:var(--lbc)}.sv.vinted{color:var(--vinted)}.sv.ebay{color:var(--ebay)}.sv.facebook{color:var(--facebook)}
.sl{font-size:.54rem;color:var(--mu);text-transform:uppercase;letter-spacing:1px;margin-top:.2rem}
.ctrl{background:var(--s2);border:1px solid var(--bd);color:var(--tx);padding:.5rem .9rem;border-radius:8px;font-size:.74rem;font-weight:700;cursor:pointer;font-family:"DM Sans",sans-serif}
.ctrl.pause{border-color:var(--steal);color:var(--steal)}
.lang-toggle{display:flex;gap:.2rem;background:var(--s2);border:1px solid var(--bd);border-radius:8px;padding:.2rem}
.lang-toggle button{background:none;border:none;color:var(--mu);padding:.35rem .6rem;border-radius:6px;font-size:.68rem;font-weight:800;cursor:pointer;font-family:"DM Sans",sans-serif}
.lang-toggle button.active{background:var(--accent);color:#000}
.scanbar{padding:.5rem 2rem;background:var(--s1);border-bottom:1px solid var(--bd);font-size:.7rem;color:var(--mu);display:flex;align-items:center;gap:.8rem;font-family:"Space Mono",monospace}
.dot{width:8px;height:8px;border-radius:50%;background:var(--accent);animation:pulse 1.5s infinite;flex-shrink:0}
.dot.idle{background:var(--mu);animation:none}
.pbar{flex:1;height:5px;background:var(--bd);border-radius:3px;overflow:hidden;max-width:300px}
.pfill{height:100%;background:var(--accent);transition:width .5s}
.searchbar{padding:1.1rem 2rem .3rem;display:flex;gap:.6rem;align-items:center}
#search{flex:1;background:var(--s2);border:1px solid var(--bd);color:var(--tx);padding:.8rem 1rem;border-radius:10px;font-size:.95rem;font-family:"DM Sans",sans-serif}
#search:focus{outline:none;border-color:var(--accent)}
.tabs{display:flex;gap:.5rem;padding:.5rem 2rem;border-bottom:1px solid var(--bd)}
.tab{background:none;border:none;color:var(--mu);padding:.5rem .9rem;font-size:.8rem;font-weight:800;cursor:pointer;border-bottom:2px solid transparent;font-family:"DM Sans",sans-serif}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.filters{display:flex;gap:.45rem;padding:.9rem 2rem 0;flex-wrap:wrap;align-items:center}
.fbtn{background:var(--s2);border:1px solid var(--bd);color:var(--mu);padding:.4rem .85rem;border-radius:999px;font-size:.68rem;font-weight:700;cursor:pointer;font-family:"DM Sans",sans-serif;white-space:nowrap}
.fbtn:hover{border-color:var(--accent)}
.fbtn.active{background:var(--accent);color:#000;border-color:var(--accent)}
.chip{display:flex;align-items:center;gap:.35rem;background:var(--s2);border:1px solid var(--bd);padding:.35rem .7rem;border-radius:999px;font-size:.68rem;cursor:pointer;user-select:none}
.chip input{accent-color:var(--accent);cursor:pointer}
.chip.on{border-color:var(--accent);color:var(--accent)}
.toolbar{display:flex;gap:1.5rem;align-items:center;padding:.9rem 2rem 0;flex-wrap:wrap}
.pricerange{display:flex;align-items:center;gap:.5rem;font-size:.7rem;color:var(--mu)}
.pricerange input{width:80px;background:var(--s2);border:1px solid var(--bd);color:var(--tx);padding:.4rem .6rem;border-radius:6px;font-family:"Space Mono",monospace;font-size:.75rem}
.pricerange input:focus{outline:none;border-color:var(--accent)}
.sortbox,.deliverybox{display:flex;align-items:center;gap:.5rem;font-size:.7rem;color:var(--mu)}
.sortbox select,.deliverybox select,.settings-select{background:var(--s2);border:1px solid var(--bd);color:var(--tx);padding:.4rem .6rem;border-radius:6px;font-size:.75rem;font-family:"DM Sans",sans-serif}
.sortbox select:focus,.deliverybox select:focus{outline:none;border-color:var(--accent)}
.src-pill{font-size:.56rem;padding:.12rem .4rem;border-radius:4px;font-weight:700;display:inline-block;margin-left:.4rem}
.src-pill.market{background:#00ff8822;color:var(--accent)}
.src-pill.pcpp{background:#ffd16622;color:var(--gold)}
.src-pill.estimate{background:#5a708033;color:var(--mu)}
.price-source-line{display:flex;align-items:center;gap:.5rem;margin-bottom:.9rem;font-size:.68rem;color:var(--mu)}
.price-source-line .src-pill{margin-left:0}
main{max-width:1500px;margin:0 auto;padding:1.2rem 2rem 4rem}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;flex-wrap:wrap;gap:.5rem}
.bar .sub{font-size:.7rem;color:var(--mu);font-family:"Space Mono",monospace}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:1rem}
.card{background:var(--s2);border:1px solid var(--bd);border-radius:12px;text-decoration:none;color:var(--tx);overflow:hidden;display:flex;flex-direction:column;transition:.2s;position:relative;cursor:pointer}
.card:hover{transform:translateY(-4px);box-shadow:0 12px 40px rgba(0,0,0,.4)}
.card.good{border-color:#00ff8844}.card.steal{border-color:var(--steal)}
tr.flash{animation:sdrowflash 1.2s}
@keyframes sdrowflash{0%{background:#3b82f626}100%{background:transparent}}
.sd-deal-card.flash{animation:sdcardflash 1.2s}
@keyframes sdcardflash{0%{box-shadow:0 0 0 2px var(--sd-accent)}100%{box-shadow:none}}
.card-img{height:135px;background:var(--bd);display:flex;align-items:center;justify-content:center;font-size:2.2rem;overflow:hidden;position:relative}
.card-img img{width:100%;height:100%;object-fit:cover}
.src-tag{position:absolute;top:8px;right:8px;font-size:.56rem;font-weight:800;padding:.2rem .5rem;border-radius:4px;text-transform:uppercase;color:#fff}
.src-tag.lbc{background:var(--lbc)}.src-tag.vinted{background:var(--vinted)}.src-tag.ebay{background:var(--ebay)}.src-tag.facebook{background:var(--facebook)}
.ship-tag{position:absolute;bottom:8px;right:8px;font-size:.54rem;font-weight:700;padding:.15rem .4rem;border-radius:4px;background:rgba(0,0,0,.55);color:#fff}
.conf-badge{position:absolute;top:8px;left:8px;font-size:.56rem;font-weight:800;padding:.2rem .45rem;border-radius:4px;color:#fff}
.conf-hi{background:#00ff88;color:#000}.conf-mid{background:#ffd166;color:#000}.conf-lo{background:#ff8c42;color:#000}
.open-hint{font-size:.6rem;color:var(--mu);margin-top:.4rem;opacity:.7}
.card-body{padding:.85rem;display:flex;flex-direction:column;gap:.35rem;flex:1}
.badge{font-size:.6rem;font-weight:800;padding:.22rem .5rem;border-radius:4px;width:fit-content;text-transform:uppercase;letter-spacing:.5px}
.badge.good{background:#00ff8822;color:var(--accent)}.badge.steal{background:var(--steal);color:#fff}
.model{font-size:.9rem;font-weight:800;display:flex;align-items:center;gap:.4rem;flex-wrap:wrap}
.func{font-size:.56rem;background:#00ff8818;color:var(--accent);padding:.1rem .4rem;border-radius:3px;font-weight:700}
.subject{font-size:.7rem;color:var(--mu);line-height:1.3}
.price-row{display:flex;align-items:baseline;gap:.6rem;margin-top:.2rem}
.price{font-family:"Space Mono",monospace;font-size:1.45rem;font-weight:700}
.margin{font-size:.7rem;color:var(--gold);font-weight:700;font-family:"Space Mono",monospace}
.ref{font-size:.6rem;color:var(--mu);font-family:"Space Mono",monospace}
.loc{font-size:.64rem;color:var(--mu)}
.flags{display:flex;flex-direction:column;gap:.2rem;margin-top:.2rem}
.flag{font-size:.6rem;color:var(--mu);line-height:1.3}
.empty{color:var(--mu);font-style:italic;padding:3rem 0;text-align:center}
.report{margin-top:.5rem;border-top:1px solid var(--bd);padding-top:.5rem}
.verdict{font-size:.72rem;font-weight:800;padding:.28rem .55rem;border-radius:5px;display:inline-block;margin-bottom:.4rem}
.verdict.excellent{background:#22c55e1a;color:#22c55e}
.verdict.good{background:#3b82f61a;color:var(--accent)}
.verdict.ok{background:#eab3081a;color:var(--gold)}
.verdict.meh{background:#8a8a9222;color:var(--mu)}
.gscore{font-family:"Space Mono",monospace;font-weight:700;margin-left:.4rem}
.subscores{display:grid;grid-template-columns:1fr 1fr;gap:.25rem;margin-top:.3rem}
.ss{display:flex;align-items:center;gap:.3rem;font-size:.6rem}
.ss .k{color:var(--mu);width:64px;flex-shrink:0}
.ss .bar{flex:1;height:5px;background:var(--bd);border-radius:3px;overflow:hidden}
.ss .fl{height:100%;border-radius:3px}
.ss .v{width:26px;text-align:right;font-family:"Space Mono",monospace}
.resell-line{font-size:.6rem;color:var(--mu);margin-top:.25rem;line-height:1.4}
.card-actions{display:flex;align-items:center;justify-content:space-between;gap:.5rem;margin-top:.5rem;padding-top:.5rem;border-top:1px solid var(--bd)}
.card-actions .lst-open{font-size:.62rem;padding:.35rem .6rem}
.card-actions .open-hint{margin-top:0}
.eval-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:.8rem}
.eval-card{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;cursor:pointer;transition:.2s}
.eval-card:hover{border-color:var(--accent)}
.eval-head{padding:.85rem 1rem;display:flex;justify-content:space-between;align-items:center;gap:.5rem}
.eval-model{font-weight:800;font-size:.92rem}
.eval-cat{font-size:.58rem;color:var(--mu);text-transform:uppercase;letter-spacing:.5px}
.eval-fair{font-family:"Space Mono",monospace;font-size:1.3rem;font-weight:700;color:var(--accent)}
.eval-bars{padding:0 1rem .85rem;display:flex;flex-direction:column;gap:.3rem}
.eval-bar{display:flex;align-items:center;gap:.5rem;font-size:.64rem;font-family:"Space Mono",monospace}
.eval-bar .lab{width:42px;color:var(--mu)}
.eval-bar .track{flex:1;height:7px;background:var(--bd);border-radius:4px;overflow:hidden}
.eval-bar .fill{height:100%;border-radius:4px}
.eval-bar .val{width:54px;text-align:right}
.sim{margin-top:.8rem;padding-top:.7rem;border-top:1px solid var(--bd)}
.sim label{font-size:.64rem;color:var(--mu);display:block;margin-bottom:.3rem}
.sim input[type=range]{width:100%}
.sim-out{display:flex;justify-content:space-between;align-items:center;margin-top:.4rem;font-family:"Space Mono",monospace;font-size:.72rem}
.sim-verdict{font-weight:800;padding:.2rem .5rem;border-radius:4px}
.card,.eval-card{cursor:pointer}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);backdrop-filter:blur(3px);z-index:200;display:flex;align-items:center;justify-content:center;padding:1.5rem}
.modal-overlay.hidden{display:none}
.modal{background:var(--s1);border:1px solid var(--bd);border-radius:16px;max-width:640px;width:100%;max-height:88vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.6)}
.modal-head{position:sticky;top:0;background:var(--s1);border-bottom:1px solid var(--bd);padding:1.2rem 1.4rem;display:flex;justify-content:space-between;align-items:flex-start;z-index:2}
.modal-title{font-size:1.15rem;font-weight:800}
.modal-catlabel{font-size:.68rem;color:var(--mu);margin-top:.2rem}
.modal-close{background:var(--s2);border:1px solid var(--bd);color:var(--tx);width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:.9rem}
.modal-close:hover{border-color:var(--steal);color:var(--steal)}
.modal-body{padding:1.4rem}
.modal-section-title{font-size:.8rem;font-weight:800;margin:1.2rem 0 .7rem;padding-top:.9rem;border-top:1px solid var(--bd)}
.listings{display:flex;flex-direction:column;gap:.45rem}
.listing-row{display:flex;align-items:center;gap:.6rem;background:var(--s2);border:1px solid var(--bd);border-radius:8px;padding:.55rem .7rem;font-size:.72rem;flex-wrap:wrap}
.listing-row .src-tag{font-size:.56rem;padding:.15rem .4rem;border-radius:4px;color:#fff;font-weight:800;position:static}
.lst-subject{flex:1;min-width:120px;color:var(--tx)}
.lst-conf{font-size:.62rem;color:var(--mu);font-family:"Space Mono",monospace}
.lst-date{font-size:.6rem;color:var(--mu);white-space:nowrap}
.lst-price{font-family:"Space Mono",monospace;font-weight:700;color:var(--accent)}
.lst-open{background:var(--accent);color:#000;text-decoration:none;font-weight:800;font-size:.65rem;padding:.3rem .6rem;border-radius:6px;white-space:nowrap}
.lst-open:hover{opacity:.85}
.settings-row{display:flex;flex-direction:column;gap:.5rem;margin-bottom:1.2rem}
.settings-row label{font-size:.72rem;color:var(--mu);font-weight:700}
.settings-platforms{display:flex;gap:.6rem;flex-wrap:wrap}
.settings-note{font-size:.65rem;color:var(--mu);margin-top:.4rem;line-height:1.5}
.settings-save{background:var(--accent);color:#000;border:none;padding:.7rem 1.2rem;border-radius:8px;font-weight:800;cursor:pointer;font-size:.8rem;width:100%;margin-top:.5rem}
footer{border-top:1px solid var(--bd);padding:1.5rem 2rem;text-align:center;font-family:"Space Mono",monospace;font-size:.6rem;color:var(--mu);line-height:1.8}
.hidden{display:none!important}
@media(max-width:600px){header,main,footer,.filters,.tabs,.searchbar,.scanbar,.toolbar{padding-left:1rem;padding-right:1rem}.stats{gap:.8rem}.sv{font-size:1rem}.modal{max-height:94vh}.modal-body{padding:1rem}}

/* ═══════════════ SOLDIER — nouvelle coquille (sidebar + pages sobres) ═══════════════ */
:root{
  --sd-bg:#0a0a0b; --sd-panel:#121214; --sd-panel2:#18181b; --sd-border:#26262a;
  --sd-text:#e8e8ea; --sd-mute:#8a8a92; --sd-accent:#3b82f6;
  --sd-green:#22c55e; --sd-amber:#eab308; --sd-red:#ef4444;
}
.sd-shell{display:flex;min-height:100vh;background:var(--sd-bg);color:var(--sd-text);font-family:Inter,system-ui,-apple-system,sans-serif}
.sd-sidebar{width:220px;flex-shrink:0;background:var(--sd-panel);border-right:1px solid var(--sd-border);display:flex;flex-direction:column;padding:1.2rem 0;position:sticky;top:0;height:100vh}
.sd-brand{font-size:1rem;font-weight:700;letter-spacing:.02em;padding:0 1.2rem 1.4rem;border-bottom:1px solid var(--sd-border);margin-bottom:.8rem}
.sd-nav{display:flex;flex-direction:column;gap:.15rem;padding:0 .6rem}
.sd-nav button{display:flex;align-items:center;gap:.6rem;background:none;border:none;color:var(--sd-mute);padding:.6rem .7rem;border-radius:6px;font-size:.82rem;font-weight:500;text-align:left;cursor:pointer;font-family:inherit}
.sd-nav button:hover{background:var(--sd-panel2);color:var(--sd-text)}
.sd-nav button.active{background:var(--sd-panel2);color:var(--sd-text);font-weight:600;box-shadow:inset 2px 0 0 var(--sd-accent)}
.sd-main{flex:1;min-width:0;padding:1.6rem 2rem 3rem}
.sd-page{display:none}
.sd-page.active{display:block}
.sd-kpirow{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--sd-border);border:1px solid var(--sd-border);border-radius:8px;overflow:hidden;margin-bottom:1.6rem}
.sd-kpi{background:var(--sd-panel);padding:1rem 1.2rem}
.sd-kpi-label{font-size:.7rem;color:var(--sd-mute);margin-bottom:.35rem;text-transform:uppercase;letter-spacing:.04em}
.sd-kpi-value{font-size:1.4rem;font-weight:700;font-variant-numeric:tabular-nums}
.sd-kpi-value.pos{color:var(--sd-green)}
.sd-kpi-value.neg{color:var(--sd-red)}
.sd-section-title{font-size:.95rem;font-weight:600;margin:1.8rem 0 .8rem}
.sd-table-wrap{border:1px solid var(--sd-border);border-radius:8px;overflow:hidden}
.sd-table{width:100%;border-collapse:collapse;font-size:.82rem}
.sd-table th{text-align:left;font-weight:600;color:var(--sd-mute);font-size:.72rem;text-transform:uppercase;letter-spacing:.03em;padding:.6rem .9rem;background:var(--sd-panel2);border-bottom:1px solid var(--sd-border)}
.sd-table td{padding:.6rem .9rem;border-bottom:1px solid var(--sd-border);font-variant-numeric:tabular-nums}
.sd-table tbody tr:last-child td{border-bottom:none}
.sd-table tbody tr:hover{background:var(--sd-panel2)}
.sd-table .num{text-align:right}
.sd-pill{display:inline-block;padding:.15rem .55rem;border-radius:4px;font-size:.7rem;font-weight:600;border:1px solid transparent}
.sd-pill.status-nouveau{background:#3b82f61a;color:var(--sd-accent);border-color:#3b82f640}
.sd-pill.status-en_route,.sd-pill.status-envoye_pipeline{background:#eab3081a;color:var(--sd-amber);border-color:#eab30840}
.sd-pill.status-recu,.sd-pill.status-en_build{background:#8a8a921a;color:var(--sd-mute);border-color:#8a8a9240}
.sd-pill.status-a_vendre,.sd-pill.status-en_cours{background:#3b82f61a;color:var(--sd-accent);border-color:#3b82f640}
.sd-pill.status-vendu{background:#22c55e1a;color:var(--sd-green);border-color:#22c55e40}
.sd-pill.status-excellent{background:#22c55e1a;color:var(--sd-green);border-color:#22c55e40}
.sd-pill.status-good{background:#3b82f61a;color:var(--sd-accent);border-color:#3b82f640}
.sd-pill.status-ok{background:#eab3081a;color:var(--sd-amber);border-color:#eab30840}
.sd-pill.status-meh{background:#ef44441a;color:var(--sd-red);border-color:#ef444440}
.sd-btn{background:var(--sd-accent);color:#fff;border:none;padding:.45rem .9rem;border-radius:6px;font-size:.78rem;font-weight:600;cursor:pointer;font-family:inherit}
.sd-btn:hover{opacity:.88}
.sd-btn.ghost{background:none;border:1px solid var(--sd-border);color:var(--sd-text)}
.sd-btn.small{padding:.3rem .6rem;font-size:.72rem}
.sd-empty{color:var(--sd-mute);font-size:.85rem;padding:2rem;text-align:center;border:1px dashed var(--sd-border);border-radius:8px}
.sd-form{display:grid;grid-template-columns:repeat(3,1fr);gap:.8rem;background:var(--sd-panel);border:1px solid var(--sd-border);border-radius:8px;padding:1.2rem;margin-bottom:1.4rem}
.sd-form label{display:flex;flex-direction:column;gap:.3rem;font-size:.72rem;color:var(--sd-mute)}
.sd-form input,.sd-form select{background:var(--sd-panel2);border:1px solid var(--sd-border);color:var(--sd-text);padding:.5rem .6rem;border-radius:6px;font-size:.8rem;font-family:inherit}
.sd-form .sd-form-actions{grid-column:1/-1;display:flex;justify-content:flex-end}
.sd-conf-badge{font-size:.68rem;font-weight:600;padding:.1rem .45rem;border-radius:4px}
.sd-conf-high{background:#22c55e1a;color:var(--sd-green)}
.sd-conf-mid{background:#eab3081a;color:var(--sd-amber)}
.sd-conf-low{background:#ef44441a;color:var(--sd-red)}
.sd-toolbar-row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-bottom:.8rem}
.sd-toolbar-row input,.sd-toolbar-row select{background:var(--sd-panel2);border:1px solid var(--sd-border);color:var(--sd-text);padding:.45rem .6rem;border-radius:6px;font-size:.78rem;font-family:inherit}
.sd-note{font-size:.72rem;color:var(--sd-mute);line-height:1.6;margin:-.6rem 0 1.4rem;max-width:640px}
.sd-thumb{width:76px;height:76px;border-radius:6px;object-fit:cover;background:var(--sd-panel2);display:block;border:1px solid var(--sd-border)}
.sd-table td:has(.sd-thumb){padding:.4rem .6rem}
.sd-deal-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem}
@media(max-width:1200px){.sd-deal-grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:860px){.sd-deal-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.sd-deal-grid{grid-template-columns:1fr}}
.sd-deal-card{background:var(--sd-panel);border:1px solid var(--sd-border);border-radius:10px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column;transition:border-color .15s}
.sd-deal-card:hover{border-color:var(--sd-accent)}
.sd-deal-img{width:100%;height:200px;background:var(--sd-panel2);display:flex;align-items:center;justify-content:center;overflow:hidden}
.sd-deal-img img{width:100%;height:100%;object-fit:cover;display:block}
.sd-deal-img.empty{color:var(--sd-mute);font-size:.7rem}
.sd-deal-body{padding:.9rem 1rem 1rem;display:flex;flex-direction:column;gap:.45rem;flex:1}
.sd-deal-top{display:flex;justify-content:space-between;align-items:flex-start;gap:.5rem}
.sd-deal-model{font-size:.92rem;font-weight:700;line-height:1.25}
.sd-deal-src{font-size:.72rem;color:var(--sd-mute)}
.sd-deal-price-row{display:flex;align-items:baseline;gap:.6rem;margin-top:.1rem}
.sd-deal-price{font-size:1.25rem;font-weight:700;font-variant-numeric:tabular-nums}
.sd-deal-margin{font-size:.78rem;font-weight:600;font-variant-numeric:tabular-nums}
.sd-deal-market{font-size:.7rem;color:var(--sd-mute)}
.sd-deal-meters{display:flex;gap:1rem;margin-top:.2rem}
.sd-deal-meter-label{font-size:.62rem;color:var(--sd-mute);text-transform:uppercase;letter-spacing:.03em;margin-bottom:.2rem}
.sd-deal-foot{display:flex;justify-content:space-between;align-items:center;margin-top:auto;padding-top:.6rem;border-top:1px solid var(--sd-border);font-size:.68rem;color:var(--sd-mute)}
.sd-tag{display:inline-block;background:var(--sd-panel2);border:1px solid var(--sd-border);color:var(--sd-mute);font-size:.65rem;padding:.05rem .4rem;border-radius:3px;margin:0 .2rem .2rem 0}
.sd-aging-slow{color:var(--sd-red);font-weight:600}
.sd-toast{position:fixed;bottom:1.2rem;right:1.2rem;background:var(--sd-panel2);border:1px solid var(--sd-border);border-left:3px solid var(--sd-accent);color:var(--sd-text);padding:.7rem 1rem;border-radius:6px;font-size:.8rem;z-index:500;max-width:340px;box-shadow:0 8px 24px rgba(0,0,0,.4);animation:sdtoastin .2s ease-out}
.sd-toast.error{border-left-color:var(--sd-red)}
@keyframes sdtoastin{0%{opacity:0;transform:translateY(8px)}100%{opacity:1;transform:translateY(0)}}
.sd-btn[disabled]{opacity:.6;cursor:not-allowed}

/* ═══════════════ ONBOARDING ═══════════════ */
.ob-overlay{position:fixed;inset:0;background:var(--sd-bg);z-index:1000;display:flex;align-items:center;justify-content:center;padding:2rem}
.ob-overlay.hidden{display:none}
.ob-card{max-width:560px;width:100%;background:var(--sd-panel);border:1px solid var(--sd-border);border-radius:12px;padding:2rem}
.ob-steps{display:flex;gap:.4rem;margin-bottom:1.6rem}
.ob-steps span{flex:1;height:3px;background:var(--sd-border);border-radius:2px}
.ob-steps span.done{background:var(--sd-accent)}
.ob-title{font-size:1.25rem;font-weight:700;margin-bottom:.4rem}
.ob-sub{font-size:.82rem;color:var(--sd-mute);margin-bottom:1.4rem;line-height:1.5}
.ob-pipeline{display:flex;flex-direction:column;gap:.6rem;margin-bottom:1rem}
.ob-pipeline-step{display:flex;gap:.7rem;align-items:flex-start;font-size:.8rem}
.ob-pipeline-step b{color:var(--sd-accent);font-family:inherit;min-width:18px}
.ob-field{margin-bottom:1rem}
.ob-field label{display:block;font-size:.75rem;color:var(--sd-mute);margin-bottom:.4rem}
.ob-field input,.ob-field select{width:100%;background:var(--sd-panel2);border:1px solid var(--sd-border);color:var(--sd-text);padding:.55rem .7rem;border-radius:6px;font-size:.82rem;font-family:inherit}
.ob-checks{display:flex;gap:.6rem;flex-wrap:wrap}
.ob-checks label{display:flex;align-items:center;gap:.4rem;background:var(--sd-panel2);border:1px solid var(--sd-border);padding:.4rem .7rem;border-radius:6px;font-size:.78rem;cursor:pointer}
.ob-choices{display:flex;flex-direction:column;gap:.6rem;margin-bottom:1rem}
.ob-choice{border:1px solid var(--sd-border);border-radius:8px;padding:.8rem 1rem;cursor:pointer}
.ob-choice:hover{border-color:var(--sd-accent)}
.ob-choice.selected{border-color:var(--sd-accent);background:#3b82f60f}
.ob-choice b{display:block;font-size:.85rem;margin-bottom:.2rem}
.ob-choice span{font-size:.72rem;color:var(--sd-mute)}
.ob-actions{display:flex;justify-content:space-between;margin-top:1.6rem}
</style>
</head>
<body>
<div id="ob-overlay" class="ob-overlay hidden">
  <div class="ob-card">
    <div class="ob-steps">
      <span id="ob-step-dot-1"></span><span id="ob-step-dot-2"></span><span id="ob-step-dot-3"></span>
      <span id="ob-step-dot-4"></span><span id="ob-step-dot-5"></span>
    </div>
    <div id="ob-step-1">
      <div class="ob-title">Bienvenue sur SOLDIER</div>
      <div class="ob-sub">Un outil local de flipping de composants et PC : le scanner trouve les deals, toi tu gères le reste.</div>
      <div class="ob-pipeline">
        <div class="ob-pipeline-step"><b>1</b><div><b>Scanner</b> — repère les deals sur Leboncoin, Vinted, eBay et Facebook.</div></div>
        <div class="ob-pipeline-step"><b>2</b><div><b>Pipeline</b> — un clic envoie un deal validé vers tes achats.</div></div>
        <div class="ob-pipeline-step"><b>3</b><div><b>Achats</b> — suis ce que tu as réellement acheté et son statut.</div></div>
        <div class="ob-pipeline-step"><b>4</b><div><b>Builds</b> — regroupe plusieurs achats en un PC complet.</div></div>
        <div class="ob-pipeline-step"><b>5</b><div><b>Ventes</b> — enregistre la revente, la marge nette se calcule seule.</div></div>
      </div>
      <div class="ob-actions"><span></span><button class="sd-btn" onclick="obGoto(2)">Commencer</button></div>
    </div>
    <div id="ob-step-2" class="hidden">
      <div class="ob-title">Tes préférences</div>
      <div class="ob-sub">Ça détermine ce que le scanner surveille pour toi. Modifiable à tout moment depuis Réglages.</div>
      <div class="ob-field"><label>Marketplaces à scanner</label>
        <div class="ob-checks">
          <label><input type="checkbox" id="ob-src-lbc" checked>Leboncoin</label>
          <label><input type="checkbox" id="ob-src-vinted" checked>Vinted</label>
          <label><input type="checkbox" id="ob-src-ebay" checked>eBay</label>
          <label><input type="checkbox" id="ob-src-facebook" checked>Facebook</label>
        </div>
      </div>
      <div class="ob-field"><label>Catégories qui t'intéressent (optionnel)</label>
        <input type="text" id="ob-categories" placeholder="ex: GPU, CPU, Boîtiers — laisse vide pour tout suivre"></div>
      <div class="ob-field"><label>Seuil de marge minimum pour alerter (€)</label>
        <input type="number" id="ob-min-margin" value="50" min="0"></div>
      <div class="ob-field"><label>Budget max par achat (€)</label>
        <input type="number" id="ob-max-budget" value="400" min="0"></div>
      <div class="ob-actions"><button class="sd-btn ghost" onclick="obGoto(1)">Retour</button><button class="sd-btn" onclick="obGoto(3)">Continuer</button></div>
    </div>
    <div id="ob-step-3" class="hidden">
      <div class="ob-title">Vérification visuelle (optionnelle)</div>
      <div class="ob-sub">Le niveau 4 anti-bourrage envoie la photo d'une annonce à l'API Anthropic pour confirmer
        que le produit correspond au modèle annoncé. Ça consomme des tokens — désactivé par défaut, avec un
        plafond mensuel que l'app ne dépassera jamais.</div>
      <div class="ob-field"><label>Activer la vérification visuelle</label>
        <select id="ob-vision-enabled"><option value="false">Non, rester sur les niveaux 1-3 (gratuits)</option><option value="true">Oui</option></select></div>
      <div class="ob-field"><label>Plafond mensuel (€)</label>
        <input type="number" id="ob-vision-budget" value="2" min="0" step="0.5"></div>
      <div class="ob-actions"><button class="sd-btn ghost" onclick="obGoto(2)">Retour</button><button class="sd-btn" onclick="obGoto(4)">Continuer</button></div>
    </div>
    <div id="ob-step-4" class="hidden">
      <div class="ob-title">Données de départ</div>
      <div class="ob-sub">Comment veux-tu démarrer ?</div>
      <div class="ob-choices">
        <div class="ob-choice" data-starter="empty" onclick="obSelectStarter('empty',this)">
          <b>Démarrer à vide</b><span>Rien n'est ajouté, le scanner commence à alimenter le pipeline dès qu'un deal correspond.</span></div>
        <div class="ob-choice" data-starter="demo" onclick="obSelectStarter('demo',this)">
          <b>Charger un jeu de données de démo</b><span>Quelques achats, un build et une vente d'exemple pour voir l'app remplie.</span></div>
        <div class="ob-choice" data-starter="import" onclick="obSelectStarter('import',this)">
          <b>Importer l'ancien stock</b><span>Colle un export JSON de l'ancien SOLDER ({"purchases":[...], "builds":[...]}).</span></div>
      </div>
      <div id="ob-import-field" class="ob-field hidden"><label>Export JSON</label>
        <textarea id="ob-import-json" rows="4" style="width:100%;background:var(--sd-panel2);border:1px solid var(--sd-border);color:var(--sd-text);border-radius:6px;padding:.6rem;font-family:inherit;font-size:.78rem"></textarea></div>
      <div class="ob-actions"><button class="sd-btn ghost" onclick="obGoto(3)">Retour</button><button class="sd-btn" id="ob-finish-btn" onclick="obFinish()">Terminer</button></div>
    </div>
    <div id="ob-step-5" class="hidden">
      <div class="ob-title">C'est prêt</div>
      <div class="ob-sub">Tes préférences sont enregistrées. Direction le dashboard.</div>
      <div class="ob-actions"><span></span><button class="sd-btn" onclick="obLand()">Aller au dashboard</button></div>
    </div>
  </div>
</div>

<div id="sd-toast-container"></div>

<div class="sd-shell">
  <nav class="sd-sidebar">
    <div class="sd-brand">SOLDIER</div>
    <div class="sd-nav">
      <button class="sd-nav-btn active" data-page="scanner" onclick="sdSwitchPage('scanner',this)">Scanner</button>
      <button class="sd-nav-btn" data-page="pipeline" onclick="sdSwitchPage('pipeline',this)">Pipeline</button>
      <button class="sd-nav-btn" data-page="purchases" onclick="sdSwitchPage('purchases',this)">Achats</button>
      <button class="sd-nav-btn" data-page="builds" onclick="sdSwitchPage('builds',this)">Builds</button>
      <button class="sd-nav-btn" data-page="sales" onclick="sdSwitchPage('sales',this)">Ventes</button>
      <button class="sd-nav-btn" data-page="analytics" onclick="sdSwitchPage('analytics',this)">Analytics</button>
      <button class="sd-nav-btn" data-page="settings" onclick="sdSwitchPage('settings',this)">Réglages avancés</button>
    </div>
  </nav>
  <div class="sd-main">

<div id="sd-page-scanner" class="sd-page active">
<header>
  <div class="logo">PC<span>Sniper</span><small id="sub">…</small></div>
  <div class="stats">
    <div class="stat"><div class="sv" id="s-deals">0</div><div class="sl" data-i18n="stat_deals">Deals</div></div>

    <div class="stat"><div class="sv steal" id="s-steals">0</div><div class="sl" data-i18n="stat_steals">Affaires or</div></div>
    <div class="stat"><div class="sv gold" id="s-margin">0€</div><div class="sl" data-i18n="stat_margin">Marge cumul.</div></div>
    <div class="stat"><div class="sv lbc" id="s-lbc">0</div><div class="sl">LBC</div></div>
    <div class="stat"><div class="sv vinted" id="s-vinted">0</div><div class="sl">Vinted</div></div>
    <div class="stat"><div class="sv ebay" id="s-ebay">0</div><div class="sl">eBay</div></div>
    <div class="stat"><div class="sv facebook" id="s-facebook">0</div><div class="sl">FB</div></div>
    <div class="stat"><div class="sv" id="s-cycle">0</div><div class="sl" data-i18n="stat_cycles">Cycles</div></div>
    <div class="lang-toggle">
      <button id="lang-fr" onclick="setLang('fr')">FR</button>
      <button id="lang-en" onclick="setLang('en')">EN</button>
    </div>
    <button class="ctrl" id="settings-btn" onclick="openSettings()" data-i18n="btn_settings">Réglages</button>
    <button class="ctrl" id="toggle" onclick="toggleScan()" data-i18n="btn_pause">Pause</button>
  </div>
</header>

<div class="scanbar">
  <span class="dot" id="dot"></span>
  <span id="scanstatus" data-i18n="scanning_init">Initialisation…</span>
  <span class="pbar"><span class="pfill" id="pfill" style="width:0%"></span></span>
</div>

<div class="searchbar">
  <input id="search" data-i18n-ph="search_placeholder" placeholder="Cherche un composant (ex: RTX 3060, Ryzen 5600, DDR4 32, B550…)" oninput="onSearch()">
</div>
<div class="tabs">
  <button class="tab active" onclick="switchTab('deals',this)"><span data-i18n="tab_deals">Deals en direct</span></button>
  <button class="tab" onclick="switchTab('eval',this)"><span data-i18n="tab_eval">Évaluateur de prix</span></button>
</div>

<div id="tab-deals">
  <div class="filters" id="deals-platform-filters">
    <span class="chip on" id="chip-leboncoin" onclick="togglePlatform('leboncoin',this)"><input type="checkbox" checked readonly>Leboncoin</span>
    <span class="chip on" id="chip-vinted" onclick="togglePlatform('vinted',this)"><input type="checkbox" checked readonly>Vinted</span>
    <span class="chip on" id="chip-ebay" onclick="togglePlatform('ebay',this)"><input type="checkbox" checked readonly>eBay</span>
    <span class="chip on" id="chip-facebook" onclick="togglePlatform('facebook',this)"><input type="checkbox" checked readonly>Facebook</span>
    <span class="chip" id="chip-steal" onclick="toggleStealOnly(this)"><input type="checkbox" readonly><span data-i18n="filter_steal_only">Affaires en or uniquement</span></span>
  </div>
  <div class="filters" id="deals-cat-filters">
    <button class="fbtn active" onclick="filterDealCat('all',this)" data-i18n="filter_all_categories">Toutes catégories</button>
  </div>
  <div class="filters" id="price-presets">
    <button class="fbtn" onclick="setPricePreset(0,50,this)">&lt; 50€</button>
    <button class="fbtn" onclick="setPricePreset(0,100,this)">&lt; 100€</button>
    <button class="fbtn" onclick="setPricePreset(0,200,this)">&lt; 200€</button>
    <button class="fbtn" onclick="setPricePreset(0,500,this)">&lt; 500€</button>
    <button class="fbtn" onclick="clearPricePreset(this)" data-i18n="filter_price_reset">Réinitialiser</button>
  </div>
  <div class="filters" id="verdict-filters">
    <span class="chip on" id="chip-v-excellent" onclick="toggleVerdict('excellent',this)"><input type="checkbox" checked readonly><span data-i18n="chip_verdict_excellent">Excellent</span></span>
    <span class="chip on" id="chip-v-good" onclick="toggleVerdict('good',this)"><input type="checkbox" checked readonly><span data-i18n="chip_verdict_good">Bon</span></span>
    <span class="chip on" id="chip-v-ok" onclick="toggleVerdict('ok',this)"><input type="checkbox" checked readonly><span data-i18n="chip_verdict_ok">Correct</span></span>
    <span class="chip on" id="chip-v-meh" onclick="toggleVerdict('meh',this)"><input type="checkbox" checked readonly><span data-i18n="chip_verdict_meh">Moyen</span></span>
  </div>
  <div class="toolbar">
    <div class="pricerange">
      <label data-i18n="label_price_min">Prix min</label><input type="number" id="price-min" placeholder="0" oninput="onSearch()">
      <label data-i18n="label_price_max">Prix max</label><input type="number" id="price-max" placeholder="∞" oninput="onSearch()">
    </div>
    <div class="deliverybox">
      <label data-i18n="label_delivery">Livraison</label>
      <select id="delivery-select" onchange="onSearch()">
        <option value="all" data-i18n="delivery_all">Peu importe</option>
        <option value="ships" data-i18n="delivery_ships">Livraison possible</option>
        <option value="pickup" data-i18n="delivery_pickup">Remise en main propre</option>
      </select>
    </div>
    <div class="deliverybox">
      <label data-i18n="label_min_score">Score min</label>
      <input type="number" id="score-min" placeholder="0" min="0" max="100" style="width:55px" oninput="onSearch()">
    </div>
    <div class="deliverybox">
      <label data-i18n="label_min_confidence">Confiance min</label>
      <input type="number" id="confidence-min" placeholder="0" min="0" max="100" style="width:55px" oninput="onSearch()">
    </div>
    <div class="sortbox">
      <label data-i18n="label_sort">Trier par</label>
      <select id="sort-select" onchange="onSearch()">
        <option value="margin_desc" data-i18n="sort_margin_desc">Marge (décroissant)</option>
        <option value="price_asc" data-i18n="sort_price_asc">Prix (croissant)</option>
        <option value="price_desc" data-i18n="sort_price_desc">Prix (décroissant)</option>
        <option value="score_desc" data-i18n="sort_score_desc">Score global (décroissant)</option>
        <option value="recent" data-i18n="sort_recent">Plus récent</option>
      </select>
    </div>
  </div>
  <main><div class="bar"><span class="sub" id="deals-sub"></span></div>
  <div class="sd-deal-grid" id="deals-grid"></div></main>
</div>

<div id="tab-eval" class="hidden">
  <div class="filters" id="cat-filters">
    <button class="fbtn active" onclick="filterCat('all',this)" data-i18n="filter_all_categories">Toutes catégories</button>
  </div>
  <main><div class="bar"><span class="sub" data-i18n="eval_hint">Clique un modèle pour voir l'évaluation + le graphique d'évolution du marché</span></div>
  <div class="sd-table-wrap">
    <table class="sd-table">
      <thead><tr>
        <th data-i18n="col_model">Modèle</th><th data-i18n="col_category">Catégorie</th>
        <th class="num" data-i18n="col_steal">Affaire en or</th><th class="num" data-i18n="col_good">Bon deal</th>
        <th class="num" data-i18n="col_fair">Prix juste</th><th data-i18n="col_source">Source</th>
      </tr></thead>
      <tbody id="eval-grid"></tbody>
    </table>
  </div></main>
</div>

<footer>
  <span data-i18n="footer_line1">SOLDIER · app locale Flask · Leboncoin + Vinted + eBay + Facebook</span><br>
  <span data-i18n="footer_line2">Seuils "affaire en or" / "bon deal" · annonces HS ou à risque d'arnaque jamais alertées</span>
</footer>
</div><!-- /sd-page-scanner -->

<!-- ═══════════════ PIPELINE ═══════════════ -->
<div id="sd-page-pipeline" class="sd-page">
  <div class="sd-kpirow" id="sd-kpirow"></div>
  <div class="sd-section-title">Deals à haute confiance</div>
  <div style="margin-bottom:.8rem">
    <label style="font-size:.75rem;color:var(--sd-mute)">
      <input type="checkbox" id="sd-show-flagged" onchange="sdLoadPipeline()"> Afficher aussi les annonces à confiance basse (flaggées)
    </label>
  </div>
  <div class="sd-table-wrap">
    <table class="sd-table">
      <thead><tr><th>Modèle</th><th>Catégorie</th><th>Source</th><th class="num">Prix</th><th class="num">Marché</th><th class="num">Marge</th><th>Confiance</th><th>Statut</th><th></th></tr></thead>
      <tbody id="sd-pipeline-tbody"></tbody>
    </table>
  </div>
</div>

<!-- ═══════════════ ACHATS ═══════════════ -->
<div id="sd-page-purchases" class="sd-page">
  <div class="sd-section-title">Nouvel achat manuel</div>
  <div class="sd-form">
    <label>Modèle<input type="text" id="pf-model" placeholder="ex: RTX 3070"></label>
    <label>Catégorie<input type="text" id="pf-category" placeholder="ex: GPU"></label>
    <label>Prix d'achat (€)<input type="number" id="pf-price" step="0.01" min="0"></label>
    <label>Frais de port (€)<input type="number" id="pf-shipping" step="0.01" value="0" min="0"></label>
    <label>Frais protection acheteur (€)<input type="number" id="pf-protection" step="0.01" value="0" min="0"></label>
    <label>Source<input type="text" id="pf-source" placeholder="ex: leboncoin"></label>
    <label>Tags (séparés par virgule)<input type="text" id="pf-tags" placeholder="ex: urgent, revente rapide"></label>
    <div class="sd-form-actions"><button class="sd-btn" id="pf-submit" onclick="sdCreatePurchase()">Ajouter l'achat</button></div>
  </div>
  <div class="sd-section-title">Tous les achats</div>
  <div class="sd-toolbar-row">
    <input type="text" id="pf-search" placeholder="Rechercher un modèle…" oninput="sdLoadPurchases()">
    <select id="pf-filter-status" onchange="sdLoadPurchases()">
      <option value="">Tous statuts</option>
      <option value="en_route">En route</option>
      <option value="recu">Reçu</option>
      <option value="en_build">En build</option>
      <option value="vendu">Vendu</option>
    </select>
    <select id="pf-sort" onchange="sdLoadPurchases()">
      <option value="date_desc">Plus récent</option>
      <option value="date_asc">Plus ancien</option>
      <option value="aging_desc">Stock le plus lent</option>
      <option value="price_desc">Prix (décroissant)</option>
    </select>
    <a class="sd-btn ghost small" href="/api/soldier/export/purchases.csv">Exporter CSV</a>
  </div>
  <div class="sd-table-wrap">
    <table class="sd-table">
      <thead><tr><th></th><th>Modèle</th><th>Catégorie</th><th class="num">Prix payé</th><th class="num">Total (port+frais)</th><th>Source</th><th>Tags</th><th>Statut</th><th>En stock depuis</th><th></th></tr></thead>
      <tbody id="sd-purchases-tbody"></tbody>
    </table>
  </div>
  <div class="sd-section-title">Corbeille (achats)</div>
  <div id="sd-purchases-trash"></div>
</div>

<!-- ═══════════════ BUILDS ═══════════════ -->
<div id="sd-page-builds" class="sd-page">
  <div class="sd-section-title">Nouveau build</div>
  <div class="sd-form">
    <label>Nom du build<input type="text" id="bf-name" placeholder="ex: PC Gaming Ryzen/RTX"></label>
    <label>Coûts additionnels (€)<input type="number" id="bf-extra" step="0.01" value="0" min="0"></label>
    <label>Prix cible (€)<input type="number" id="bf-target" step="0.01" min="0"></label>
    <div class="sd-form-actions"><button class="sd-btn" id="bf-submit" onclick="sdCreateBuild()">Créer le build</button></div>
  </div>
  <div id="sd-builds-list"></div>
</div>

<!-- ═══════════════ VENTES ═══════════════ -->
<div id="sd-page-sales" class="sd-page">
  <div class="sd-section-title">Nouvelle vente</div>
  <div class="sd-form">
    <label>Achat ou build à vendre<select id="sf-target" onchange="sdSuggestSalePrice()"></select></label>
    <label>Prix de vente (€)<input type="number" id="sf-price" step="0.01" min="0"></label>
    <label>Plateforme<input type="text" id="sf-platform" placeholder="ex: leboncoin"></label>
    <label>Frais (€)<input type="number" id="sf-fees" step="0.01" value="0" min="0"></label>
    <div class="sd-form-actions" style="justify-content:space-between;align-items:center">
      <span id="sf-suggestion" style="font-size:.72rem;color:var(--sd-mute)"></span>
      <button class="sd-btn" id="sf-submit" onclick="sdCreateSale()">Enregistrer la vente</button>
    </div>
  </div>
  <div class="sd-section-title">Historique des ventes</div>
  <div class="sd-toolbar-row">
    <select id="sf-filter-platform" onchange="sdLoadSales()">
      <option value="">Toutes plateformes</option>
    </select>
    <select id="sf-sort" onchange="sdLoadSales()">
      <option value="date_desc">Plus récent</option>
      <option value="date_asc">Plus ancien</option>
      <option value="margin_desc">Marge (décroissant)</option>
    </select>
    <a class="sd-btn ghost small" href="/api/soldier/export/sales.csv">Exporter CSV</a>
  </div>
  <div class="sd-table-wrap">
    <table class="sd-table">
      <thead><tr><th>Article</th><th class="num">Prix de vente</th><th>Plateforme</th><th class="num">Frais</th><th class="num">Marge nette</th><th>Date</th></tr></thead>
      <tbody id="sd-sales-tbody"></tbody>
    </table>
  </div>
</div>

<!-- ═══════════════ ANALYTICS ═══════════════ -->
<div id="sd-page-analytics" class="sd-page">
  <div class="sd-section-title">Revenu et marge par mois</div>
  <div class="sd-table-wrap" style="padding:1rem;background:var(--sd-panel)">
    <canvas id="analytics-chart" height="90"></canvas>
  </div>
  <div class="sd-section-title">Marge par catégorie</div>
  <div class="sd-table-wrap">
    <table class="sd-table">
      <thead><tr><th>Catégorie</th><th class="num">Ventes</th><th class="num">Marge cumulée</th></tr></thead>
      <tbody id="sd-analytics-category"></tbody>
    </table>
  </div>
</div>

<!-- ═══════════════ RÉGLAGES (étendus) ═══════════════ -->
<div id="sd-page-settings" class="sd-page">
  <div class="sd-section-title">Seuils et budget de flip</div>
  <div class="sd-form">
    <label>Marge minimum pour alerter (€)<input type="number" id="pref-min-margin" step="1" min="0"></label>
    <label>Budget max par achat (€)<input type="number" id="pref-max-budget" step="1" min="0"></label>
    <div class="sd-form-actions"><button class="sd-btn" onclick="sdSavePreferences()">Enregistrer les seuils</button></div>
  </div>

  <div class="sd-section-title">Vérification visuelle (niveau 4, optionnelle)</div>
  <div class="sd-form">
    <label>Activée
      <select id="vision-enabled"><option value="false">Non</option><option value="true">Oui</option></select>
    </label>
    <label>Plafond mensuel (€)<input type="number" id="vision-budget" step="0.5" min="0"></label>
    <div class="sd-form-actions" style="justify-content:space-between;align-items:center">
      <span id="vision-spent" style="font-size:.72rem;color:var(--sd-mute)"></span>
      <button class="sd-btn" onclick="sdSaveVisionSettings()">Enregistrer</button>
    </div>
  </div>
  <div class="sd-note">La vérification visuelle envoie la photo de l'annonce à l'API Anthropic pour confirmer
    que le produit correspond au modèle annoncé — elle consomme des tokens et ne se déclenche jamais au-delà
    du plafond mensuel choisi ici. Les réglages de scan (pays, plateformes, langue) restent dans le bouton
    "Réglages" du Scanner, en haut de la page.</div>

  <div class="sd-section-title">Export et configuration</div>
  <div style="display:flex;gap:.6rem;flex-wrap:wrap">
    <a class="sd-btn ghost small" href="/api/soldier/export/purchases.csv">Exporter les achats (CSV)</a>
    <a class="sd-btn ghost small" href="/api/soldier/export/sales.csv">Exporter les ventes (CSV)</a>
    <button class="sd-btn ghost small" onclick="sdRestartOnboarding()">Relancer la configuration</button>
  </div>
</div>


<div id="modal-overlay" class="modal-overlay hidden" onclick="if(event.target===this)closeDetail()">
  <div class="modal">
    <div class="modal-head">
      <div><div id="modal-title" class="modal-title"></div><div id="modal-cat" class="modal-catlabel"></div></div>
      <button class="modal-close" onclick="closeDetail()">✕</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<div id="settings-overlay" class="modal-overlay hidden" onclick="if(event.target===this)closeSettings()">
  <div class="modal" style="max-width:440px">
    <div class="modal-head">
      <div class="modal-title" data-i18n="settings_title">Paramètres</div>
      <button class="modal-close" onclick="closeSettings()">✕</button>
    </div>
    <div class="modal-body">
      <div class="settings-row">
        <label data-i18n="settings_lang_label">Langue de l'interface</label>
        <div class="lang-toggle" style="width:fit-content">
          <button id="settings-lang-fr" onclick="setLang('fr')">Français</button>
          <button id="settings-lang-en" onclick="setLang('en')">English</button>
        </div>
      </div>
      <div class="settings-row">
        <label data-i18n="settings_country_label">Pays (source des annonces)</label>
        <select id="settings-country" class="settings-select"></select>
      </div>
      <div class="settings-row">
        <label data-i18n="settings_location_label">Ta ville (pour Facebook Marketplace)</label>
        <input type="text" id="settings-location" class="settings-select" data-i18n-ph="settings_location_ph" placeholder="ex: Annecy, Chambéry, 74000...">
        <div class="settings-note" data-i18n="settings_location_note">Facebook cherche uniquement autour d'un point précis (pas dans tout le pays) — sans ta ville, il cherche par défaut autour de la capitale, ce qui peut être très loin de chez toi.</div>
        <div style="display:flex;align-items:center;gap:.5rem;margin-top:.6rem">
          <label style="margin:0;white-space:nowrap" data-i18n="settings_radius_label">Rayon de recherche</label>
          <select id="settings-radius" class="settings-select">
            <option value="8">8 km</option>
            <option value="16">16 km</option>
            <option value="32">32 km</option>
            <option value="50">50 km</option>
            <option value="80">80 km</option>
            <option value="160">160 km</option>
          </select>
        </div>
      </div>
      <div class="settings-row">
        <label data-i18n="settings_platforms_label">Plateformes actives</label>
        <div class="settings-platforms">
          <span class="chip on" id="settings-chip-lbc" onclick="toggleSettingsPlatform('lbc',this)"><input type="checkbox" readonly>Leboncoin</span>
          <span class="chip on" id="settings-chip-vinted" onclick="toggleSettingsPlatform('vinted',this)"><input type="checkbox" readonly>Vinted</span>
          <span class="chip on" id="settings-chip-ebay" onclick="toggleSettingsPlatform('ebay',this)"><input type="checkbox" readonly>eBay</span>
          <span class="chip on" id="settings-chip-facebook" onclick="toggleSettingsPlatform('facebook',this)"><input type="checkbox" readonly>Facebook</span>
        </div>
        <div class="settings-note" data-i18n="settings_saved_note">Leboncoin n'est disponible qu'en France. Un changement redémarre le scan automatiquement (sans relancer l'app) avec la nouvelle configuration.</div>
      </div>
      <button class="settings-save" onclick="saveSettingsPanel()" data-i18n="settings_save">Enregistrer</button>
    </div>
  </div>
</div>

<script>
// ═══════════════════ I18N ═══════════════════
// Libellés de catégorie traduits (le backend renvoie la clé stable, ex "GPU" ;
// ce tableau fournit le libellé affiché dans chaque langue)
const CAT_LABELS = {
  fr: {GPU:"Cartes graphiques", CPU:"Processeurs", MOBO:"Cartes mères", RAM:"Mémoire RAM",
       STORAGE:"Stockage", PSU:"Alimentations", COOLING:"Refroidissement", CASE:"Boîtiers",
       MONITOR:"Écrans", KEYBOARD:"Claviers", MOUSE:"Souris", HEADSET:"Casques",
       LAPTOP:"PC portables", CHAIR:"Sièges gaming"},
  en: {GPU:"Graphics cards", CPU:"Processors", MOBO:"Motherboards", RAM:"RAM memory",
       STORAGE:"Storage", PSU:"Power supplies", COOLING:"Cooling", CASE:"Cases",
       MONITOR:"Monitors", KEYBOARD:"Keyboards", MOUSE:"Mice", HEADSET:"Headsets",
       LAPTOP:"Laptops", CHAIR:"Gaming chairs"},
};
function catLabel(code){ return (CAT_LABELS[LANG] && CAT_LABELS[LANG][code]) || code; }

const I18N = {
fr: {
  stat_deals:"Deals", stat_steals:"Affaires or", stat_margin:"Marge cumul.", stat_cycles:"Cycles",
  scanning_init:"Initialisation…",
  scanning_status:"Scan: {cat} · {model}",
  scanning_paused:"En pause",
  scanning_done:"Cycle {cycle} terminé · {new} nouveau(x) deal(s) · prochaine passe imminente",
  search_placeholder:"Cherche un composant (ex: RTX 3060, Ryzen 5600, DDR4 32, B550…)",
  tab_deals:"Deals en direct", tab_eval:"Évaluateur de prix",
  btn_settings:"Réglages", btn_pause:"Pause", btn_resume:"Lecture",
  filter_all_categories:"Toutes catégories",
  filter_steal_only:"Affaires en or uniquement",
  chip_verdict_excellent:"Excellent", chip_verdict_good:"Bon", chip_verdict_ok:"Correct", chip_verdict_meh:"Moyen",
  col_verdict:"Verdict", col_model:"Modèle", col_marketplace:"Marketplace", col_price:"Prix",
  col_market_price:"Prix marché", col_margin:"Marge", col_confidence:"Confiance", col_scam_score:"Score anti-scam",
  col_date:"Date", col_category:"Catégorie", col_steal:"Affaire en or", col_good:"Bon deal", col_fair:"Prix juste",
  col_source:"Source",
  label_price_min:"Prix min", label_price_max:"Prix max",
  label_delivery:"Livraison", delivery_all:"Peu importe", delivery_ships:"Livraison possible", delivery_pickup:"Remise en main propre",
  label_min_score:"Score min", label_min_confidence:"Confiance min", filter_price_reset:"Réinitialiser",
  label_sort:"Trier par", sort_margin_desc:"Marge (décroissant)", sort_price_asc:"Prix (croissant)",
  sort_price_desc:"Prix (décroissant)", sort_score_desc:"Score global (décroissant)", sort_recent:"Plus récent",
  deals_sub:"{shown}/{total} deal(s) affiché(s) · live",
  empty_deals:"Aucun deal ne correspond à ces filtres.",
  empty_deals_none:"Aucun deal pour le moment. Le scan tourne…",
  eval_hint:"Clique un modèle pour voir l'évaluation + le graphique d'évolution du marché",
  footer_line1:"SOLDIER · app locale Flask · Leboncoin + Vinted + eBay + Facebook",
  footer_line2:"Seuils \"affaire en or\" / \"bon deal\" · annonces HS ou à risque d'arnaque jamais alertées",
  open_listing:"Ouvrir l'annonce", open_hint:"fiche complète",
  modal_sim_label:"Simule un prix d'achat — vois le verdict en direct :",
  modal_history_title:"Historique des prix vus (marché)",
  modal_listings_title:"Annonces actuelles ({count})",
  modal_no_history:"Pas encore d'historique pour ce modèle.<br>Le graphique se construit au fil des cycles de scan.",
  modal_no_listings:"Aucune annonce actuellement détectée pour ce modèle précis. Le scan continue en fond.",
  stat_min:"Min vu", stat_med:"Médian", stat_max:"Max vu", stat_obs:"Observations",
  settings_title:"Paramètres", settings_lang_label:"Langue de l'interface",
  settings_country_label:"Pays (source des annonces)", settings_platforms_label:"Plateformes actives",
  settings_location_label:"Ta ville (pour Facebook Marketplace)",
  settings_location_ph:"ex: Annecy, Chambéry, 74000...",
  settings_location_note:"Facebook cherche uniquement autour d'un point précis (pas dans tout le pays) — sans ta ville, il cherche par défaut autour de la capitale, ce qui peut être très loin de chez toi.",
  settings_radius_label:"Rayon de recherche",
  settings_saved_note:"Leboncoin n'est disponible qu'en France. Un changement redémarre le scan automatiquement (sans relancer l'app) avec la nouvelle configuration.",
  settings_save:"Enregistrer", settings_saved:"Enregistré, le scan redémarre avec la nouvelle config…",
  verdict_excellent:"À SAISIR", verdict_good:"BON DEAL", verdict_ok:"CORRECT", verdict_meh:"MOYEN / À NÉGOCIER",
  badge_steal:"AFFAIRE EN OR", badge_good:"BON DEAL",
  sub_deal:"deal", sub_resale:"revente", sub_perfprice:"perf/€", sub_demand:"demande",
  resell_line:"Revente: marge {margin}€ ({marginpct}%) · demande {demand}/100 · fraîcheur {freshness}/100",
  conf_hi:"bon état", conf_mid:"à vérifier", conf_lo:"peu d'info",
  src_market:"marché réel", src_pcpp:"PCPartPicker", src_estimate:"estimation",
  note_market:"basé sur {n} annonces observées (min {min}€ / max {max}€)",
  note_pcpp:"neuf {new_price}€ (PCPartPicker, {date}) × décote âge",
  note_estimate:"estimation de départ, pas encore confirmée par le marché ou PCPartPicker",
  ship_yes:"Livraison", ship_no:"Main propre",
  flag_accessory:"Accessoire détecté ({type}) — pas le composant lui-même",
  flag_bad_condition:"État probablement HS/défectueux (« {word} »)",
  flag_good_condition:"Bon état confirmé (« {word} »)",
  flag_unknown_short:"État non précisé + description très courte — demande photos/test avant achat",
  flag_unknown:"État non explicitement confirmé — à vérifier avant achat",
  flag_scam:"Pattern arnaque (contact hors plateforme)",
  flag_scam_high:"Risque d'arnaque élevé (score {score}/100) — annonce écartée",
  flag_scam_moderate:"Signaux suspects détectés (score {score}/100) — grande prudence recommandée",
  acc_type_box:"carton/boîte seule", acc_type_cable:"câble/adaptateur",
  acc_type_water:"waterblock/refroidissement seul", acc_type_support:"support/accessoire",
  acc_type_search:"recherche/achat",
},
en: {
  stat_deals:"Deals", stat_steals:"Steal deals", stat_margin:"Total margin", stat_cycles:"Cycles",
  scanning_init:"Initializing…",
  scanning_status:"Scanning: {cat} · {model}",
  scanning_paused:"Paused",
  scanning_done:"Cycle {cycle} done · {new} new deal(s) · next pass coming up",
  search_placeholder:"Search a component (e.g. RTX 3060, Ryzen 5600, DDR4 32, B550…)",
  tab_deals:"Live deals", tab_eval:"Price evaluator",
  btn_settings:"Settings", btn_pause:"Pause", btn_resume:"Resume",
  filter_all_categories:"All categories",
  filter_steal_only:"Steal deals only",
  chip_verdict_excellent:"Excellent", chip_verdict_good:"Good", chip_verdict_ok:"Fair", chip_verdict_meh:"Mediocre",
  col_verdict:"Verdict", col_model:"Model", col_marketplace:"Marketplace", col_price:"Price",
  col_market_price:"Market price", col_margin:"Margin", col_confidence:"Confidence", col_scam_score:"Scam score",
  col_date:"Date", col_category:"Category", col_steal:"Steal deal", col_good:"Good deal", col_fair:"Fair price",
  col_source:"Source",
  label_price_min:"Min price", label_price_max:"Max price",
  label_delivery:"Shipping", delivery_all:"Any", delivery_ships:"Shipping available", delivery_pickup:"Local pickup only",
  label_min_score:"Min score", label_min_confidence:"Min confidence", filter_price_reset:"Reset",
  label_sort:"Sort by", sort_margin_desc:"Margin (highest first)", sort_price_asc:"Price (lowest first)",
  sort_price_desc:"Price (highest first)", sort_score_desc:"Overall score (highest first)", sort_recent:"Most recent",
  deals_sub:"{shown}/{total} deal(s) shown · live",
  empty_deals:"No deal matches these filters.",
  empty_deals_none:"No deals yet. Scan in progress…",
  eval_hint:"Click a model to see its full evaluation + market price chart",
  footer_line1:"SOLDIER · local Flask app · Leboncoin + Vinted + eBay + Facebook",
  footer_line2:"\"Steal\" / \"good deal\" thresholds · broken or likely-scam listings never alerted",
  open_listing:"Open listing", open_hint:"full sheet",
  modal_sim_label:"Simulate a purchase price — see the verdict live:",
  modal_history_title:"Observed price history (market)",
  modal_listings_title:"Current listings ({count})",
  modal_no_history:"No price history yet for this model.<br>The chart builds up over scan cycles.",
  modal_no_listings:"No listing currently detected for this exact model. The scan keeps running in the background.",
  stat_min:"Min seen", stat_med:"Median", stat_max:"Max seen", stat_obs:"Observations",
  settings_title:"Settings", settings_lang_label:"Interface language",
  settings_country_label:"Country (listings source)", settings_platforms_label:"Active platforms",
  settings_location_label:"Your city (for Facebook Marketplace)",
  settings_location_ph:"e.g. Annecy, Denver, 80202...",
  settings_location_note:"Facebook only searches around a specific point (not the whole country) — without your city, it defaults to searching around the capital, which could be very far from you.",
  settings_radius_label:"Search radius",
  settings_saved_note:"Leboncoin is only available in France. Any change automatically restarts the scan (without relaunching the app) with the new configuration.",
  settings_save:"Save", settings_saved:"Saved, the scan is restarting with the new config…",
  verdict_excellent:"GRAB IT", verdict_good:"GOOD DEAL", verdict_ok:"FAIR", verdict_meh:"MEDIOCRE / NEGOTIATE",
  badge_steal:"STEAL DEAL", badge_good:"GOOD DEAL",
  sub_deal:"deal", sub_resale:"resale", sub_perfprice:"perf/€", sub_demand:"demand",
  resell_line:"Resale: margin {margin}€ ({marginpct}%) · demand {demand}/100 · freshness {freshness}/100",
  conf_hi:"good condition", conf_mid:"to verify", conf_lo:"little info",
  src_market:"real market", src_pcpp:"PCPartPicker", src_estimate:"estimate",
  note_market:"based on {n} observed listings (min {min}€ / max {max}€)",
  note_pcpp:"new {new_price}€ (PCPartPicker, {date}) × age discount",
  note_estimate:"starting estimate, not yet confirmed by the market or PCPartPicker",
  ship_yes:"Ships", ship_no:"Pickup only",
  flag_accessory:"Accessory detected ({type}) — not the component itself",
  flag_bad_condition:"Likely broken/defective (\"{word}\")",
  flag_good_condition:"Good condition confirmed (\"{word}\")",
  flag_unknown_short:"Condition not stated + very short description — ask for photos/a test before buying",
  flag_unknown:"Condition not explicitly confirmed — verify before buying",
  flag_scam:"Scam pattern (off-platform contact)",
  flag_scam_high:"High scam risk (score {score}/100) — listing discarded",
  flag_scam_moderate:"Suspicious signals detected (score {score}/100) — extra caution advised",
  acc_type_box:"box/empty box only", acc_type_cable:"cable/adapter",
  acc_type_water:"waterblock/cooling only", acc_type_support:"stand/accessory",
  acc_type_search:"wanted/buying ad",
}
};
// Table de correspondance des mots FR détectés -> clé de traduction anglaise
// (les listes de détection restent en français ; ceci ne sert qu'à l'affichage)
const WORD_EN = {
  "hs":"HS/dead","h.s":"HS/dead","ne fonctionne pas":"doesn't work","ne marche pas":"doesn't work",
  "pour pièce":"for parts","pour pieces":"for parts","pour pièces":"for parts","défectueux":"defective",
  "defectueux":"defective","en panne":"broken","panne":"broken","cassé":"broken","casse":"broken",
  "ne démarre pas":"won't boot","ne demarre pas":"won't boot","ne boot pas":"won't boot",
  "à réparer":"needs repair","a reparer":"needs repair","vendu en l'état":"sold as-is","vendu en l etat":"sold as-is",
  "ne s'allume pas":"won't turn on","ne s allume pas":"won't turn on","artefact":"artifacting","artefacts":"artifacting",
  "écran cassé":"broken screen","ecran casse":"broken screen","bloqué":"stuck","bloque":"stuck",
  "problème":"issue","probleme":"issue","défaut":"fault","defaut":"fault","grillé":"fried","grille":"fried",
  "fumée":"smoke","fumee":"smoke",
  "fonctionne parfaitement":"works perfectly","parfait état":"perfect condition","parfait etat":"perfect condition",
  "excellent état":"excellent condition","excellent etat":"excellent condition","très bon état":"very good condition",
  "tres bon etat":"very good condition","comme neuf":"like new","état neuf":"new condition","etat neuf":"new condition",
  "neuf":"new","testé":"tested","teste":"tested","testée":"tested","testee":"tested","sous garantie":"under warranty",
  "garantie":"warranty","facture":"receipt","rien à signaler":"nothing to report","ras":"nothing to report",
  "impeccable":"flawless","nickel":"spotless","aucun problème":"no issues","aucun probleme":"no issues",
  "fonctionnel":"functional","opérationnel":"operational","operationnel":"operational",
  "jamais miné":"never mined","jamais mine":"never mined","non miné":"not mined","peu servi":"lightly used",
  "peu utilisé":"lightly used",
};
const ACC_TYPE_KEY = {
  "carton/boîte seule":"acc_type_box","câble/adaptateur":"acc_type_cable",
  "waterblock/refroidissement seul":"acc_type_water","support/accessoire":"acc_type_support",
  "recherche/achat":"acc_type_search",
};

let LANG = 'fr';
function t(key, params){
  let s = (I18N[LANG] && I18N[LANG][key]) || (I18N.fr[key]) || key;
  if(params){ for(const k in params){ s = s.replaceAll('{'+k+'}', params[k]); } }
  return s;
}
function trWord(w){
  if(LANG==='en') return WORD_EN[w] || w;
  return w;
}
function applyStaticI18n(){
  document.documentElement.lang = LANG;
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    el.textContent = t(el.getAttribute('data-i18n'));
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{
    el.placeholder = t(el.getAttribute('data-i18n-ph'));
  });
  document.querySelectorAll('[data-cat-code]').forEach(el=>{
    el.textContent = catLabel(el.dataset.catCode);
  });
  document.getElementById('lang-fr').classList.toggle('active', LANG==='fr');
  document.getElementById('lang-en').classList.toggle('active', LANG==='en');
  const sfr=document.getElementById('settings-lang-fr'), sen=document.getElementById('settings-lang-en');
  if(sfr){sfr.classList.toggle('active', LANG==='fr'); sen.classList.toggle('active', LANG==='en');}
}
async function setLang(lang){
  LANG = lang;
  localStorage.setItem('sniper_lang', lang);
  applyStaticI18n();
  renderDeals(); renderEval();
  try{ await jpost('/api/settings', {lang}); }catch(e){}
}

// ═══════════════ ÉTAT GLOBAL ═══════════════
let CATALOG=[], CATEGORIES={}, DEALS=[], knownUrls=new Set();
let srcFilter={leboncoin:true,vinted:true,ebay:true,facebook:true}, stealOnly=false, catFilter='all', firstLoad=true;
let verdictFilter={excellent:true,good:true,ok:true,meh:true};
let COUNTRIES_LIST=[];

// ═══════════════ RÉSEAU / ERREURS / FORMATAGE ═══════════════
function sdToast(message, isError){
  const el=document.createElement('div');
  el.className='sd-toast'+(isError?' error':'');
  el.textContent=message;
  document.getElementById('sd-toast-container').appendChild(el);
  setTimeout(()=>el.remove(), 4500);
}
async function jget(u){
  try{
    const r=await fetch(u);
    const data=await r.json().catch(()=>({}));
    if(!r.ok){ sdToast(data.error||`Erreur ${r.status}`, true); throw new Error(data.error||String(r.status)); }
    return data;
  }catch(e){ if(!(e instanceof Error && e.message.includes('Erreur'))) sdToast('Connexion au serveur impossible', true); throw e; }
}
async function jpost(u,b){
  try{
    const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    const data=await r.json().catch(()=>({}));
    if(!r.ok){ sdToast(data.error||`Erreur ${r.status}`, true); throw new Error(data.error||String(r.status)); }
    return data;
  }catch(e){ if(!(e instanceof Error && e.message.includes('Erreur'))) sdToast('Connexion au serveur impossible', true); throw e; }
}
async function withLoading(btn, fn){
  if(!btn) return fn();
  const original=btn.textContent;
  btn.disabled=true; btn.dataset.loading='1'; btn.textContent='…';
  try{ return await fn(); }
  finally{ btn.disabled=false; btn.textContent=original; }
}
function fmtEuro(v){
  if(v==null||isNaN(v)) return '—';
  return Number(v).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})+' €';
}
function fmtDateRel(ts){
  if(!ts) return '—';
  const diff=(Date.now()/1000)-ts;
  if(diff<3600) return `il y a ${Math.max(1,Math.round(diff/60))} min`;
  if(diff<86400) return `il y a ${Math.round(diff/3600)} h`;
  if(diff<7*86400) return `il y a ${Math.round(diff/86400)} j`;
  return new Date(ts*1000).toLocaleDateString('fr-FR');
}

async function boot(){
  const savedLang = localStorage.getItem('sniper_lang');
  if(savedLang) LANG = savedLang;

  try{
    const ob = await jget('/api/onboarding/status');
    if(!ob.onboarded){ obShow(); } else { window._visionStatus = ob.vision; }
  }catch(e){ /* déjà notifié par jget */ }

  const cat=await jget('/api/catalog');
  CATALOG=cat.catalog; CATEGORIES=cat.categories;

  const cf=document.getElementById('cat-filters');
  for(const [k,v] of Object.entries(CATEGORIES)){
    const b=document.createElement('button');b.className='fbtn';b.textContent=catLabel(k);b.dataset.catCode=k;
    b.onclick=()=>filterCat(k,b);cf.appendChild(b);
  }
  const dcf=document.getElementById('deals-cat-filters');
  for(const [k,v] of Object.entries(CATEGORIES)){
    const b=document.createElement('button');b.className='fbtn';b.textContent=catLabel(k);b.dataset.catCode=k;
    b.onclick=()=>filterDealCat(k,b);dcf.appendChild(b);
  }

  const countriesData = await jget('/api/countries');
  COUNTRIES_LIST = countriesData.countries;
  const sel = document.getElementById('settings-country');
  sel.innerHTML = COUNTRIES_LIST.map(c=>`<option value="${c.code}">${c.label}</option>`).join('');

  const settings = await jget('/api/settings');
  if(!savedLang && settings.lang) LANG = settings.lang;
  sel.value = settings.country || 'FR';
  document.getElementById('settings-location').value = settings.location || '';
  document.getElementById('settings-radius').value = settings.radius_km || 16;
  setPlatformChip('settings-chip-lbc', settings.sources?.lbc !== false);
  setPlatformChip('settings-chip-vinted', settings.sources?.vinted !== false);
  setPlatformChip('settings-chip-ebay', settings.sources?.ebay !== false);
  setPlatformChip('settings-chip-facebook', settings.sources?.facebook !== false);

  applyStaticI18n();
  renderEval();
  await refresh();
  setInterval(refresh, 4000);
}

// ═══════════════ SCAN STATUS / STATS ═══════════════
async function refresh(){
  const st=await jget('/api/state');
  DEALS=await jget('/api/deals');
  const steals=DEALS.filter(d=>d.tier==='steal').length;
  const margin=DEALS.reduce((s,d)=>s+(d.margin>0?d.margin:0),0);
  const lbc=DEALS.filter(d=>d.source==='leboncoin').length;
  const vinted=DEALS.filter(d=>d.source==='vinted').length;
  const ebay=DEALS.filter(d=>d.source==='ebay').length;
  const facebook=DEALS.filter(d=>d.source==='facebook').length;
  document.getElementById('s-deals').textContent=DEALS.length;
  document.getElementById('s-steals').textContent=steals;
  document.getElementById('s-margin').textContent=fmtEuro(margin);
  document.getElementById('s-lbc').textContent=lbc;
  document.getElementById('s-vinted').textContent=vinted;
  document.getElementById('s-ebay').textContent=ebay;
  document.getElementById('s-facebook').textContent=facebook;
  document.getElementById('s-cycle').textContent=st.cycle;
  document.getElementById('sub').textContent=`${CATALOG.length} models · ${st.last_update||'—'}`;

  const dot=document.getElementById('dot');
  const ss=document.getElementById('scanstatus');
  if(!st.running){dot.classList.add('idle');ss.textContent=t('scanning_paused');document.getElementById('pfill').style.width='0%';}
  else if(st.scanning){dot.classList.remove('idle');ss.textContent=t('scanning_status',{cat:st.current_cat,model:st.current_model});document.getElementById('pfill').style.width=st.progress+'%';}
  else{dot.classList.remove('idle');ss.textContent=t('scanning_done',{cycle:st.cycle,new:st.last_cycle_new});document.getElementById('pfill').style.width='100%';}
  document.getElementById('toggle').textContent=st.running?t('btn_pause'):t('btn_resume');
  document.getElementById('toggle').className='ctrl'+(st.running?'':' pause');
  renderDeals();
}

// ═══════════════ HELPERS DE RENDU ═══════════════
function sdMeterColor(v){return v>=70?'var(--sd-green)':v>=40?'var(--sd-amber)':'var(--sd-red)';}
function sdMeterColorInv(v){return v<=20?'var(--sd-green)':v<=50?'var(--sd-amber)':'var(--sd-red)';}
function sdMeterCell(value, invert){
  if(value==null) return '<span style="color:var(--sd-mute)">—</span>';
  const color = invert ? sdMeterColorInv(value) : sdMeterColor(value);
  return `<div style="display:flex;align-items:center;gap:.4rem">
    <span style="font-variant-numeric:tabular-nums;color:${color};font-weight:600;font-size:.78rem">${value}</span>
    <span style="width:34px;height:3px;background:var(--sd-border);border-radius:2px;overflow:hidden;display:inline-block">
      <span style="display:block;height:100%;width:${Math.max(4,value)}%;background:${color}"></span>
    </span>
  </div>`;
}
function scoreColor(v){return v>=80?'var(--sd-amber)':v>=65?'var(--sd-green)':v>=50?'var(--sd-amber)':'var(--sd-red)';}
function subScore(k,v){return `<div class="ss"><span class="k">${k}</span><span class="bar"><span class="fl" style="width:${Math.max(4,v)}%;background:${scoreColor(v)}"></span></span><span class="v">${v}</span></div>`;}
function verdictText(cls){return {excellent:t('verdict_excellent'),good:t('verdict_good'),ok:t('verdict_ok'),meh:t('verdict_meh')}[cls]||cls;}
function renderFlag(f){
  if(typeof f === 'string') return f; // ancien format (archive), on l'affiche tel quel
  const key = f.key, p = f.params || {};
  if(key==='accessory'){
    const typeKey = ACC_TYPE_KEY[p.type];
    return t('flag_accessory', {type: typeKey ? t(typeKey) : p.type});
  }
  if(key==='bad_condition') return t('flag_bad_condition', {word: trWord(p.word)});
  if(key==='good_condition') return t('flag_good_condition', {word: trWord(p.word)});
  if(key==='unknown_short') return t('flag_unknown_short');
  if(key==='unknown') return t('flag_unknown');
  if(key==='scam') return t('flag_scam');
  if(key==='scam_high') return t('flag_scam_high', {score: p.score});
  if(key==='scam_moderate') return t('flag_scam_moderate', {score: p.score});
  return f.fr || '';
}
function reportHTML(r){
  if(!r) return '';
  const d=r.resell_detail||{};
  let subs = subScore(t('sub_deal'),r.deal_score)+subScore(t('sub_resale'),r.resell_score);
  if(r.perf_per_price!=null) subs+=subScore(t('sub_perfprice'),Math.min(100,Math.round(r.perf_per_price)));
  subs+=subScore(t('sub_demand'),d.demande||0);
  const resell=`<div class="resell-line">${t('resell_line',{margin:d.marge_eur||0,marginpct:d.marge_pct||0,demand:d.demande||0,freshness:d.fraicheur||0})}</div>`;
  return `<div class="report">
    <span class="verdict ${r.verdict_class}">${verdictText(r.verdict_class)}<span class="gscore">${r.global_score}/100</span></span>
    <div class="subscores">${subs}</div>${resell}</div>`;
}
function srcPill(source){
  const map={"marché réel":["market","src_market"],"PCPartPicker":["pcpp","src_pcpp"],"estimation":["estimate","src_estimate"]};
  const [cls,key]=map[source]||["estimate","src_estimate"];
  return `<span class="src-pill ${cls}">${t(key)}</span>`;
}
function priceNote(ref){
  if(!ref) return '';
  const p = ref.note_params || {};
  if(ref.note_key==='market') return t('note_market', {n:p.n, min:p.min, max:p.max});
  if(ref.note_key==='pcpp') return t('note_pcpp', {new_price:p.new_price, date:p.date});
  if(ref.note_key==='estimate') return t('note_estimate');
  return ref.note || ''; // repli si donnée ancienne sans note_key
}
function escJs(s){return (s||'').replace(/'/g,"\\'");}
// Échappement HTML: toute donnée d'origine externe (titre d'annonce scrapé sur
// une marketplace, modèle/tag saisi à la main...) est un attaquant potentiel —
// une annonce titrée "<img src=x onerror=...>" ne doit jamais s'exécuter dans
// le dashboard. Utilise le moteur d'échappement natif du navigateur (fiable,
// pas de regex maison à trous) plutôt que d'insérer la chaîne brute en HTML.
function esc(s){
  const d=document.createElement('div');
  d.textContent = (s==null?'':String(s));
  return d.innerHTML;
}

// ═══════════════ LIGNES DEALS (table sobre) ═══════════════
function dealCard(d){
  const SRC_MAP={leboncoin:'Leboncoin',vinted:'Vinted',ebay:'eBay',facebook:'Facebook'};
  const srcLabel=SRC_MAP[d.source]||d.source;
  const vclass=d.report?.verdict_class||(d.tier==='steal'?'excellent':'good');
  const marginColor=d.margin>0?'var(--sd-green)':'var(--sd-red)';
  const isNew=!knownUrls.has(d.url);
  const ship=d.ships===true?t('ship_yes'):d.ships===false?t('ship_no'):'';
  const img=d.image
    ?`<img src="${esc(d.image)}" loading="lazy" onerror="this.parentElement.classList.add('empty');this.remove()">`
    :'';
  return `<div class="sd-deal-card ${isNew&&!firstLoad?'flash':''}" onclick="openDetail('${d.category}','${escJs(d.model)}')" title="${esc(d.subject||'')}">
    <div class="sd-deal-img${d.image?'':' empty'}">${img}</div>
    <div class="sd-deal-body">
      <div class="sd-deal-top">
        <div>
          <span class="sd-pill status-${vclass}">${verdictText(vclass)}</span>
          <div class="sd-deal-model">${esc(d.model)}</div>
          <div class="sd-deal-src">${esc(srcLabel)}${ship?` · ${esc(ship)}`:''}</div>
        </div>
      </div>
      <div class="sd-deal-price-row">
        <span class="sd-deal-price">${fmtEuro(d.price)}</span>
        <span class="sd-deal-margin" style="color:${marginColor}">${d.margin>0?'+':''}${fmtEuro(d.margin)}</span>
      </div>
      <div class="sd-deal-market">${t('col_market_price')}: ${fmtEuro(d.fair)}</div>
      <div class="sd-deal-meters">
        <div><div class="sd-deal-meter-label">${t('col_confidence')}</div>${sdMeterCell(d.confidence)}</div>
        <div><div class="sd-deal-meter-label">${t('col_scam_score')}</div>${sdMeterCell(d.scam_risk, true)}</div>
      </div>
      <div class="sd-deal-foot">
        <span>${d.found_at||''}</span>
        <a href="${d.url}" target="_blank" onclick="event.stopPropagation()" class="sd-btn ghost small">${t('open_listing')}</a>
      </div>
    </div>
  </div>`;
}
function renderDeals(){
  applyDealFilters();
  DEALS.forEach(d=>knownUrls.add(d.url));
  firstLoad=false;
}
function togglePlatform(src, el){
  srcFilter[src] = !srcFilter[src];
  el.classList.toggle('on', srcFilter[src]);
  el.querySelector('input').checked = srcFilter[src];
  applyDealFilters();
}
function toggleStealOnly(el){
  stealOnly = !stealOnly;
  el.classList.toggle('on', stealOnly);
  el.querySelector('input').checked = stealOnly;
  applyDealFilters();
}
function setPricePreset(min,max,btn){
  document.getElementById('price-min').value=min>0?min:'';
  document.getElementById('price-max').value=max;
  document.querySelectorAll('#price-presets .fbtn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  applyDealFilters();
}
function clearPricePreset(btn){
  document.getElementById('price-min').value='';
  document.getElementById('price-max').value='';
  document.querySelectorAll('#price-presets .fbtn').forEach(b=>b.classList.remove('active'));
  applyDealFilters();
}
function toggleVerdict(v,el){
  verdictFilter[v] = !verdictFilter[v];
  el.classList.toggle('on', verdictFilter[v]);
  el.querySelector('input').checked = verdictFilter[v];
  applyDealFilters();
}
function filterDealCat(c,btn){document.querySelectorAll('#deals-cat-filters .fbtn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');catFilter=c;applyDealFilters();}
function applyDealFilters(){
  const q=document.getElementById('search').value.toLowerCase();
  const pmin=parseFloat(document.getElementById('price-min').value);
  const pmax=parseFloat(document.getElementById('price-max').value);
  const sortBy=document.getElementById('sort-select').value;
  const delivery=document.getElementById('delivery-select').value;
  const scoreMin=parseFloat(document.getElementById('score-min').value);
  const confMin=parseFloat(document.getElementById('confidence-min').value);

  let filtered=DEALS.filter(d=>{
    if(!srcFilter[d.source]) return false;
    if(stealOnly && d.tier!=='steal') return false;
    if(catFilter!=='all' && d.category!==catFilter) return false;
    if(!isNaN(pmin) && d.price<pmin) return false;
    if(!isNaN(pmax) && d.price>pmax) return false;
    if(delivery==='ships' && d.ships!==true) return false;
    if(delivery==='pickup' && d.ships!==false) return false;
    if(!isNaN(scoreMin) && (d.report?.global_score||0)<scoreMin) return false;
    if(!isNaN(confMin) && (d.confidence??0)<confMin) return false;
    const vclass=d.report?.verdict_class;
    if(vclass && verdictFilter[vclass]===false) return false;
    if(q){
      const hay=((d.model||'')+' '+(d.subject||'')).toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  });

  const sorters={
    margin_desc:(a,b)=>(b.margin||0)-(a.margin||0),
    price_asc:(a,b)=>a.price-b.price,
    price_desc:(a,b)=>b.price-a.price,
    score_desc:(a,b)=>(b.report?.global_score||0)-(a.report?.global_score||0),
    recent:(a,b)=>(b.posted_ts||b.ts||0)-(a.posted_ts||a.ts||0),
  };
  filtered.sort(sorters[sortBy]||sorters.margin_desc);

  const grid=document.getElementById('deals-grid');
  grid.innerHTML=filtered.length?filtered.map(dealCard).join(''):`<div class="sd-empty" style="grid-column:1/-1">${DEALS.length?t('empty_deals'):t('empty_deals_none')}</div>`;
  document.getElementById('deals-sub').textContent=t('deals_sub',{shown:filtered.length,total:DEALS.length});
}

// ═══════════════ ÉVALUATEUR (table sobre) ═══════════════
function evalRow(it){
  return `<tr data-cat="${it.cat}" data-model="${(it.model||'').toLowerCase()}" onclick="openDetail('${it.cat}','${escJs(it.model)}')">
    <td>${it.model}</td>
    <td>${catLabel(it.cat)}</td>
    <td class="num">${fmtEuro(it.steal)}</td>
    <td class="num">${fmtEuro(it.good)}</td>
    <td class="num">${fmtEuro(it.fair)}</td>
    <td>${srcPill(it.source)}</td>
  </tr>`;
}
function renderEval(){document.getElementById('eval-grid').innerHTML=CATALOG.map(evalRow).join('');applyEvalFilters();}
function filterCat(c,btn){document.querySelectorAll('#tab-eval .fbtn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');window._evalCat=c;applyEvalFilters();}
function applyEvalFilters(){
  const q=document.getElementById('search').value.toLowerCase();
  const cf=window._evalCat||'all';
  document.querySelectorAll('#eval-grid tr').forEach(r=>{
    let ok=true;
    if(cf!=='all')ok=r.dataset.cat===cf;
    if(ok&&q)ok=r.dataset.model.includes(q);
    r.style.display=ok?'table-row':'none';
  });
}

// ═══════════════ FICHE DÉTAIL (modal) ═══════════════
let modalChart=null, simTimer=null;
function closeDetail(){document.getElementById('modal-overlay').classList.add('hidden');}

async function openDetail(cat, model){
  const ov=document.getElementById('modal-overlay');
  const body=document.getElementById('modal-body');
  const it=CATALOG.find(x=>x.cat===cat&&x.model===model);
  document.getElementById('modal-title').textContent=model;
  document.getElementById('modal-cat').textContent=catLabel(cat);
  body.innerHTML='<div class="no-hist">…</div>';
  ov.classList.remove('hidden');

  const data=await jget(`/api/model_detail?cat=${encodeURIComponent(cat)}&model=${encodeURIComponent(model)}`);
  if(data.error){body.innerHTML='<div class="no-hist">—</div>';return;}
  const ref=data.ref;
  const maxv=ref.fair*1.1, pct=v=>Math.max(6,Math.round(v/maxv*100));

  const listingsHTML = data.listings.length ? data.listings.map(l=>{
    const SRC_MAP={leboncoin:'Leboncoin',vinted:'Vinted',ebay:'eBay',facebook:'Facebook'};
    const srcLabel=SRC_MAP[l.source]||l.source;
    const scls=l.source==='leboncoin'?'lbc':l.source;
    return `<div class="listing-row">
      <span class="src-tag ${scls}">${srcLabel}</span>
      <span class="lst-subject">${esc((l.subject||'').slice(0,55))}</span>
      <span class="lst-conf">${l.confidence!=null?l.confidence+'%':''}</span>
      <span class="lst-date">${l.date_is_real?'':'~'}${l.found_at||''}</span>
      <span class="lst-price">${l.price}€</span>
      <a class="lst-open" href="${l.url}" target="_blank" onclick="event.stopPropagation()">${t('open_listing')}</a>
    </div>`;
  }).join('') : `<div class="no-hist">${t('modal_no_listings')}</div>`;

  body.innerHTML = `
    <div class="price-source-line">${srcPill(ref.source)}<span>${priceNote(ref)}</span></div>
    <div class="eval-bars" style="margin-bottom:1rem">
      <div class="eval-bar"><span class="lab">${LANG==='en'?'steal':'or'}</span><span class="track"><span class="fill" style="width:${pct(ref.steal)}%;background:var(--sd-red)"></span></span><span class="val">${fmtEuro(ref.steal)}</span></div>
      <div class="eval-bar"><span class="lab">deal</span><span class="track"><span class="fill" style="width:${pct(ref.good)}%;background:var(--sd-accent)"></span></span><span class="val">${fmtEuro(ref.good)}</span></div>
      <div class="eval-bar"><span class="lab">${LANG==='en'?'fair':'juste'}</span><span class="track"><span class="fill" style="width:${pct(ref.fair)}%;background:var(--sd-amber)"></span></span><span class="val">${fmtEuro(ref.fair)}</span></div>
    </div>
    <div id="modal-report">${reportHTML(data.report)}</div>
    <div class="sim" id="modal-sim"></div>
    <div class="modal-section-title">${t('modal_history_title')}</div>
    <div id="modal-stats" class="eval-stats"></div>
    <div class="chart-wrap" style="height:220px">${data.history.length?'<canvas id="modal-chart"></canvas>':'<div class="no-hist">'+t('modal_no_history')+'</div>'}</div>
    <div class="modal-section-title">${t('modal_listings_title',{count:data.listings.length})}</div>
    <div class="listings">${listingsHTML}</div>
  `;

  const lo=Math.round(ref.steal*0.6), hi=Math.round(ref.fair*1.3);
  document.getElementById('modal-sim').innerHTML=`
    <label>${t('modal_sim_label')}</label>
    <input type="range" min="${lo}" max="${hi}" value="${ref.good}" id="modal-rng" oninput="modalSimUpdate('${cat}','${escJs(model)}')">
    <div class="sim-out"><span id="modal-rngval">${fmtEuro(ref.good)}</span><span class="sim-verdict" id="modal-rngverd">…</span></div>
    <div id="modal-rngrep" style="margin-top:.5rem"></div>`;
  modalSimUpdate(cat, model);

  if(data.history.length){
    const days=data.history.map(h=>h.day), med=data.history.map(h=>h.med), mn=data.history.map(h=>h.min), mx=data.history.map(h=>h.max);
    const gmin=Math.min(...mn), gmax=Math.max(...mx);
    const sm=med.slice().sort((a,b)=>a-b), gmed=sm[Math.floor(sm.length/2)];
    const n=data.history.reduce((s,h)=>s+h.n,0);
    document.getElementById('modal-stats').innerHTML=`
      <div class="es"><div class="esv" style="color:var(--sd-red)">${fmtEuro(gmin)}</div><div class="esl">${t('stat_min')}</div></div>
      <div class="es"><div class="esv" style="color:var(--sd-amber)">${fmtEuro(gmed)}</div><div class="esl">${t('stat_med')}</div></div>
      <div class="es"><div class="esv" style="color:var(--sd-green)">${fmtEuro(gmax)}</div><div class="esl">${t('stat_max')}</div></div>
      <div class="es"><div class="esv">${n}</div><div class="esl">${t('stat_obs')}</div></div>`;
    if(modalChart){modalChart.destroy();modalChart=null;}
    const ctx=document.getElementById('modal-chart');
    if(ctx){
      modalChart=new Chart(ctx,{type:'line',data:{labels:days,datasets:[
        {label:t('stat_med'),data:med,borderColor:'#eab308',backgroundColor:'#eab30822',tension:.3,fill:true,pointRadius:2},
        {label:t('stat_min'),data:mn,borderColor:'#ef4444',tension:.3,pointRadius:1,borderDash:[4,4]},
        {label:t('stat_max'),data:mx,borderColor:'#22c55e',tension:.3,pointRadius:1,borderDash:[4,4]}]},
        options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#e8e8ea',font:{size:10}}}},
        scales:{x:{ticks:{color:'#8a8a92',font:{size:9}},grid:{color:'#26262a'}},y:{ticks:{color:'#8a8a92',font:{size:9},callback:v=>v+'€'},grid:{color:'#26262a'}}}}});
    }
  }
}
function modalSimUpdate(cat, model){
  const rng=document.getElementById('modal-rng'); if(!rng) return;
  const price=rng.value;
  document.getElementById('modal-rngval').textContent=fmtEuro(price);
  clearTimeout(simTimer);
  simTimer=setTimeout(async()=>{
    const data=await jget(`/api/evaluate?cat=${encodeURIComponent(cat)}&model=${encodeURIComponent(model)}&price=${price}`);
    if(data.report){
      const v=document.getElementById('modal-rngverd');
      v.textContent=verdictText(data.report.verdict_class)+' '+data.report.global_score+'/100';
      v.style.background=scoreColor(data.report.global_score);
      v.style.color=data.report.global_score>=80?'#000':'#fff';
      document.getElementById('modal-rngrep').innerHTML=reportHTML(data.report);
    }
  },120);
}

// ═══════════════ PARAMÈTRES (pays / plateformes) ═══════════════
let settingsPlatforms = {lbc:true, vinted:true, ebay:true, facebook:true};
function setPlatformChip(id, on){
  settingsPlatforms[id.replace('settings-chip-','')] = on;
  const el=document.getElementById(id);
  if(el){ el.classList.toggle('on', on); el.querySelector('input').checked=on; }
}
function toggleSettingsPlatform(key, el){
  settingsPlatforms[key] = !settingsPlatforms[key];
  el.classList.toggle('on', settingsPlatforms[key]);
  el.querySelector('input').checked = settingsPlatforms[key];
}
function openSettings(){ document.getElementById('settings-overlay').classList.remove('hidden'); }
function closeSettings(){ document.getElementById('settings-overlay').classList.add('hidden'); }
async function saveSettingsPanel(){
  const country = document.getElementById('settings-country').value;
  const location = document.getElementById('settings-location').value.trim();
  const radius_km = parseInt(document.getElementById('settings-radius').value, 10);
  await jpost('/api/settings', {country, location, radius_km, sources: settingsPlatforms, lang: LANG});
  closeSettings();
  refresh();
}

// ═══════════════ TABS / RECHERCHE / CONTRÔLES ═══════════════
function switchTab(t,btn){document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));btn.classList.add('active');
  document.getElementById('tab-deals').classList.toggle('hidden',t!=='deals');
  document.getElementById('tab-eval').classList.toggle('hidden',t!=='eval');}
function onSearch(){applyDealFilters();applyEvalFilters();}
async function toggleScan(){const st=await jget('/api/state');await jpost('/api/control',{action:st.running?'pause':'resume'});refresh();}

// ═══════════════ SOLDIER — navigation + pages Pipeline/Achats/Builds/Ventes/Analytics/Réglages ═══════════════
function sdSwitchPage(page, btn){
  document.querySelectorAll('.sd-nav-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.sd-page').forEach(p=>p.classList.remove('active'));
  document.getElementById('sd-page-'+page).classList.add('active');
  if(page==='pipeline') sdLoadPipeline();
  if(page==='purchases') sdLoadPurchases();
  if(page==='builds') sdLoadBuilds();
  if(page==='sales') sdLoadSales();
  if(page==='analytics') sdLoadAnalytics();
  if(page==='settings') sdLoadSettingsPage();
}

function sdConfBadge(score){
  const cls = score>=80?'sd-conf-high':score>=50?'sd-conf-mid':'sd-conf-low';
  return `<span class="sd-conf-badge ${cls}">${score}</span>`;
}
function sdPill(status){ return `<span class="sd-pill status-${status}">${(status||'').replace(/_/g,' ')}</span>`; }
function sdEuro(v){ return fmtEuro(v); }
function sdTags(tags){
  if(!tags) return '';
  return tags.split(',').map(t=>t.trim()).filter(Boolean).map(t=>`<span class="sd-tag">${esc(t)}</span>`).join('');
}

async function sdLoadKpis(){
  const k = await jget('/api/soldier/dashboard');
  document.getElementById('sd-kpirow').innerHTML = `
    <div class="sd-kpi"><div class="sd-kpi-label">Cash engagé</div><div class="sd-kpi-value">${sdEuro(k.cash_engaged)}</div></div>
    <div class="sd-kpi"><div class="sd-kpi-label">Marge réalisée</div><div class="sd-kpi-value ${k.margin_realized>=0?'pos':'neg'}">${sdEuro(k.margin_realized)}</div></div>
    <div class="sd-kpi"><div class="sd-kpi-label">ROI</div><div class="sd-kpi-value">${k.roi_percent}%</div></div>
    <div class="sd-kpi"><div class="sd-kpi-label">Valeur stock</div><div class="sd-kpi-value">${sdEuro(k.stock_value)}</div></div>
    <div class="sd-kpi"><div class="sd-kpi-label">Deals du jour</div><div class="sd-kpi-value">${k.deals_today}</div></div>
  `;
}

async function sdLoadPipeline(){
  await sdLoadKpis();
  const showFlagged = document.getElementById('sd-show-flagged').checked;
  const minConf = showFlagged ? 0 : 70;
  const listings = await jget(`/api/soldier/listings?min_confidence=${minConf}&status=nouveau`);
  const tbody = document.getElementById('sd-pipeline-tbody');
  if(!listings.length){
    tbody.innerHTML = `<tr><td colspan="9"><div class="sd-empty">Aucun deal en attente. Lance un scan et envoie un deal ici.</div></td></tr>`;
    return;
  }
  tbody.innerHTML = listings.map(l => `
    <tr>
      <td>${esc(l.model)}</td>
      <td>${esc(l.category)}</td>
      <td>${esc(l.marketplace)}</td>
      <td class="num">${sdEuro(l.price)}</td>
      <td class="num">${sdEuro(l.market_price)}</td>
      <td class="num">${sdEuro(l.estimated_margin)}</td>
      <td>${sdConfBadge(l.confidence_score)}</td>
      <td>${sdPill(l.status)}</td>
      <td><button class="sd-btn small" onclick="sdSendToPipeline(${l.id}, this)">Envoyer au pipeline</button></td>
    </tr>`).join('');
}

async function sdSendToPipeline(listingId, btn){
  await withLoading(btn, ()=>jpost('/api/soldier/pipeline/send', {listing_id: listingId}));
  sdToast('Deal envoyé au pipeline');
  sdLoadPipeline();
}

async function sdCreatePurchase(){
  const btn=document.getElementById('pf-submit');
  const data = {
    model: document.getElementById('pf-model').value.trim(),
    category: document.getElementById('pf-category').value.trim(),
    buy_price: parseFloat(document.getElementById('pf-price').value),
    shipping_cost: parseFloat(document.getElementById('pf-shipping').value) || 0,
    buyer_protection_fee: parseFloat(document.getElementById('pf-protection').value) || 0,
    source: document.getElementById('pf-source').value.trim(),
    tags: document.getElementById('pf-tags').value.trim(),
  };
  if(!data.model || !(data.buy_price > 0)){ sdToast('Modèle et prix (> 0) requis', true); return; }
  try{
    await withLoading(btn, ()=>jpost('/api/soldier/purchases', data));
  }catch(e){ return; }
  document.getElementById('pf-model').value='';
  document.getElementById('pf-price').value='';
  document.getElementById('pf-tags').value='';
  sdToast('Achat ajouté');
  sdLoadPurchases();
}

async function sdLoadPurchases(){
  const status = document.getElementById('pf-filter-status').value;
  const q = document.getElementById('pf-search').value.toLowerCase();
  const sortBy = document.getElementById('pf-sort').value;
  let purchases = await jget('/api/soldier/purchases' + (status ? `?status=${status}` : ''));
  if(q) purchases = purchases.filter(p => (p.model||'').toLowerCase().includes(q));
  const sorters = {
    date_desc: (a,b)=>b.purchase_date-a.purchase_date,
    date_asc: (a,b)=>a.purchase_date-b.purchase_date,
    aging_desc: (a,b)=>b.days_in_stock-a.days_in_stock,
    price_desc: (a,b)=>b.buy_price-a.buy_price,
  };
  purchases.sort(sorters[sortBy]||sorters.date_desc);

  const tbody = document.getElementById('sd-purchases-tbody');
  if(!purchases.length){
    tbody.innerHTML = `<tr><td colspan="10"><div class="sd-empty">Aucun achat enregistré. Ajoute-en un ou pousse un deal depuis le Pipeline.</div></td></tr>`;
  } else {
    tbody.innerHTML = purchases.map(p => {
      const total = (p.buy_price||0)+(p.shipping_cost||0)+(p.buyer_protection_fee||0);
      const slow = p.status!=='vendu' && p.days_in_stock>=30;
      const thumb = p.image_url ? `<img class="sd-thumb" src="${esc(p.image_url)}" loading="lazy" onerror="this.style.visibility='hidden'">` : '';
      return `<tr>
        <td>${thumb}</td>
        <td>${esc(p.model)}</td>
        <td>${esc(p.category)||'—'}</td>
        <td class="num">${sdEuro(p.buy_price)}</td>
        <td class="num">${sdEuro(total)}</td>
        <td>${esc(p.source)||'—'}</td>
        <td>${sdTags(p.tags)}</td>
        <td>${sdPill(p.status)}</td>
        <td class="${slow?'sd-aging-slow':''}">${p.status==='vendu'?'—':p.days_in_stock+' j'}</td>
        <td><button class="sd-btn ghost small" onclick="sdDeletePurchase(${p.id}, this)">Supprimer</button></td>
      </tr>`;
    }).join('');
  }
  sdLoadPurchasesTrash();
}

async function sdLoadPurchasesTrash(){
  const trash = await jget('/api/soldier/trash');
  const el = document.getElementById('sd-purchases-trash');
  if(!trash.purchases.length){ el.innerHTML = `<div class="sd-empty">Corbeille vide.</div>`; return; }
  el.innerHTML = `<div class="sd-table-wrap"><table class="sd-table"><tbody>` +
    trash.purchases.map(p => `<tr><td>${esc(p.model)}</td><td class="num">${sdEuro(p.buy_price)}</td>
      <td><button class="sd-btn small" onclick="sdRestorePurchase(${p.id})">Restaurer</button>
      <button class="sd-btn ghost small" onclick="sdPurgePurchase(${p.id})">Supprimer définitivement</button></td></tr>`).join('') +
    `</tbody></table></div>`;
}
async function sdRestorePurchase(id){ await jpost(`/api/soldier/purchases/${id}/restore`, {}); sdToast('Achat restauré'); sdLoadPurchases(); }
async function sdPurgePurchase(id){
  if(!confirm('Supprimer définitivement cet achat ? Cette action est irréversible.')) return;
  await fetch(`/api/soldier/purchases/${id}/purge`, {method:'DELETE'});
  sdToast('Achat supprimé définitivement');
  sdLoadPurchases();
}

async function sdDeletePurchase(id, btn){
  if(!confirm('Déplacer cet achat vers la corbeille ?')) return;
  await withLoading(btn, ()=>fetch(`/api/soldier/purchases/${id}`, {method:'DELETE'}));
  sdToast('Achat déplacé vers la corbeille');
  sdLoadPurchases();
}

async function sdCreateBuild(){
  const btn=document.getElementById('bf-submit');
  const name = document.getElementById('bf-name').value.trim();
  if(!name){ sdToast('Nom requis', true); return; }
  const data = {
    name,
    extra_costs: parseFloat(document.getElementById('bf-extra').value) || 0,
    target_price: parseFloat(document.getElementById('bf-target').value) || null,
  };
  try{ await withLoading(btn, ()=>jpost('/api/soldier/builds', data)); }catch(e){ return; }
  document.getElementById('bf-name').value='';
  sdToast('Build créé');
  sdLoadBuilds();
}

async function sdLoadBuilds(){
  const builds = await jget('/api/soldier/builds');
  const purchases = await jget('/api/soldier/purchases');
  const available = purchases.filter(p => !p.build_id && p.status !== 'vendu');
  const el = document.getElementById('sd-builds-list');
  if(!builds.length){
    el.innerHTML = `<div class="sd-empty">Aucun build. Crée-en un pour regrouper plusieurs achats en PC complet.</div>`;
    return;
  }
  el.innerHTML = builds.map(b => `
    <div class="sd-table-wrap" style="margin-bottom:1rem">
      <table class="sd-table">
        <thead><tr><th>${esc(b.name)}</th><th></th><th class="num">Coût total: ${sdEuro(b.total_cost)}</th><th>${sdPill(b.status)}</th><th></th></tr></thead>
        <tbody>
          ${(b.components||[]).map(c => `<tr><td>${esc(c.model)}</td><td colspan="2"></td><td class="num">${sdEuro(c.buy_price)}</td>
            <td><button class="sd-btn ghost small" onclick="sdDetachFromBuild(${c.id})">Retirer</button></td></tr>`).join('')}
          <tr><td colspan="5">
            <select id="build-add-${b.id}" style="margin-right:.5rem">
              <option value="">— ajouter un achat en stock —</option>
              ${available.map(p=>`<option value="${p.id}">${esc(p.model)} (${sdEuro(p.buy_price)})</option>`).join('')}
            </select>
            <button class="sd-btn small" onclick="sdAttachToBuild(${b.id})">Ajouter</button>
            <button class="sd-btn ghost small" onclick="sdDeleteBuild(${b.id})" style="float:right">Supprimer le build</button>
          </td></tr>
        </tbody>
      </table>
    </div>`).join('');
}

async function sdAttachToBuild(buildId){
  const sel = document.getElementById('build-add-'+buildId);
  const purchaseId = sel.value;
  if(!purchaseId) return;
  await jpost(`/api/soldier/builds/${buildId}/attach`, {purchase_id: parseInt(purchaseId)});
  sdLoadBuilds();
}
async function sdDetachFromBuild(purchaseId){
  await jpost(`/api/soldier/purchases/${purchaseId}/detach`, {});
  sdLoadBuilds();
}
async function sdDeleteBuild(buildId){
  if(!confirm('Déplacer ce build vers la corbeille ? Les composants repassent en stock.')) return;
  await fetch(`/api/soldier/builds/${buildId}`, {method:'DELETE'});
  sdToast('Build déplacé vers la corbeille');
  sdLoadBuilds();
}

async function sdLoadSales(){
  const purchases = await jget('/api/soldier/purchases');
  const builds = await jget('/api/soldier/builds');
  const sel = document.getElementById('sf-target');
  const availablePurchases = purchases.filter(p => p.status !== 'vendu' && !p.build_id);
  const availableBuilds = builds.filter(b => b.status !== 'vendu');
  sel.innerHTML = '<option value="">— sélectionner —</option>' +
    availablePurchases.map(p=>`<option value="purchase-${p.id}">${esc(p.model)} (achat)</option>`).join('') +
    availableBuilds.map(b=>`<option value="build-${b.id}">${esc(b.name)} (build)</option>`).join('');

  let sales = await jget('/api/soldier/sales');
  const platformFilter = document.getElementById('sf-filter-platform').value;
  const platformSel = document.getElementById('sf-filter-platform');
  const knownPlatforms = [...new Set(sales.map(s=>s.platform).filter(Boolean))];
  platformSel.innerHTML = '<option value="">Toutes plateformes</option>' +
    knownPlatforms.map(p=>`<option value="${esc(p)}" ${p===platformFilter?'selected':''}>${esc(p)}</option>`).join('');
  if(platformFilter) sales = sales.filter(s=>s.platform===platformFilter);
  const sortBy = document.getElementById('sf-sort').value;
  const sorters = {
    date_desc:(a,b)=>b.sale_date-a.sale_date, date_asc:(a,b)=>a.sale_date-b.sale_date,
    margin_desc:(a,b)=>(b.net_margin||0)-(a.net_margin||0),
  };
  sales.sort(sorters[sortBy]||sorters.date_desc);

  const tbody = document.getElementById('sd-sales-tbody');
  if(!sales.length){
    tbody.innerHTML = `<tr><td colspan="6"><div class="sd-empty">Aucune vente. Ta marge réalisée s'affichera ici dès la première revente.</div></td></tr>`;
    return;
  }
  tbody.innerHTML = sales.map(s => `
    <tr>
      <td>${s.purchase_id ? 'Achat #'+s.purchase_id : 'Build #'+s.build_id}</td>
      <td class="num">${sdEuro(s.sale_price)}</td>
      <td>${esc(s.platform)||'—'}</td>
      <td class="num">${sdEuro(s.fees)}</td>
      <td class="num" style="color:${s.net_margin>=0?'var(--sd-green)':'var(--sd-red)'}">${sdEuro(s.net_margin)}</td>
      <td>${fmtDateRel(s.sale_date)}</td>
    </tr>`).join('');
}

async function sdSuggestSalePrice(){
  const target = document.getElementById('sf-target').value;
  const hint = document.getElementById('sf-suggestion');
  hint.textContent = '';
  if(!target || !target.startsWith('purchase-')) return;
  const id = target.split('-')[1];
  try{
    const s = await jget(`/api/soldier/purchases/${id}/suggest_price`);
    document.getElementById('sf-price').value = s.suggested_price;
    hint.textContent = `Suggestion: ${sdEuro(s.suggested_price)} (coût total ${sdEuro(s.total_cost)})`;
  }catch(e){ /* déjà notifié */ }
}

async function sdCreateSale(){
  const btn=document.getElementById('sf-submit');
  const target = document.getElementById('sf-target').value;
  if(!target){ sdToast('Sélectionne un achat ou un build', true); return; }
  const [kind, id] = target.split('-');
  const price = parseFloat(document.getElementById('sf-price').value);
  if(!(price > 0)){ sdToast('Prix de vente requis (> 0)', true); return; }
  const data = {
    sale_price: price,
    platform: document.getElementById('sf-platform').value.trim(),
    fees: parseFloat(document.getElementById('sf-fees').value) || 0,
  };
  if(kind==='purchase') data.purchase_id = parseInt(id); else data.build_id = parseInt(id);
  try{ await withLoading(btn, ()=>jpost('/api/soldier/sales', data)); }catch(e){ return; }
  document.getElementById('sf-price').value=''; document.getElementById('sf-platform').value='';
  sdToast('Vente enregistrée — le statut est passé à "vendu" automatiquement');
  sdLoadSales();
}

// ═══════════════ ANALYTICS ═══════════════
let analyticsChart=null;
async function sdLoadAnalytics(){
  const a = await jget('/api/soldier/analytics');
  const months = a.by_month.map(m=>m.month);
  const revenue = a.by_month.map(m=>m.revenue);
  const margin = a.by_month.map(m=>m.margin);
  if(analyticsChart){ analyticsChart.destroy(); analyticsChart=null; }
  const ctx = document.getElementById('analytics-chart');
  if(months.length && ctx){
    analyticsChart = new Chart(ctx, {type:'bar', data:{labels:months, datasets:[
      {label:'Revenu', data:revenue, backgroundColor:'#3b82f688'},
      {label:'Marge', data:margin, backgroundColor:'#22c55e88'},
    ]}, options:{responsive:true, plugins:{legend:{labels:{color:'#e8e8ea'}}},
      scales:{x:{ticks:{color:'#8a8a92'},grid:{color:'#26262a'}},y:{ticks:{color:'#8a8a92',callback:v=>v+'€'},grid:{color:'#26262a'}}}}});
  } else if(ctx){
    ctx.getContext('2d').clearRect(0,0,ctx.width,ctx.height);
  }
  const tbody = document.getElementById('sd-analytics-category');
  tbody.innerHTML = a.by_category.length ? a.by_category.map(c => `
    <tr><td>${esc(c.category)}</td><td class="num">${c.count}</td>
      <td class="num" style="color:${c.margin>=0?'var(--sd-green)':'var(--sd-red)'}">${sdEuro(c.margin)}</td></tr>
  `).join('') : `<tr><td colspan="3"><div class="sd-empty">Pas encore de vente pour calculer la marge par catégorie.</div></td></tr>`;
}

// ═══════════════ RÉGLAGES (étendus) ═══════════════
async function sdLoadSettingsPage(){
  const status = await jget('/api/onboarding/status');
  const prefs = status.preferences || {};
  document.getElementById('pref-min-margin').value = prefs.min_margin ?? 50;
  document.getElementById('pref-max-budget').value = prefs.max_budget ?? 400;
  document.getElementById('vision-enabled').value = String(!!status.vision.enabled);
  document.getElementById('vision-budget').value = status.vision.budget_eur || 0;
  document.getElementById('vision-spent').textContent =
    `Dépensé ce mois-ci: ${sdEuro(status.vision.spent_eur)} / ${sdEuro(status.vision.budget_eur)}`;
}
async function sdSavePreferences(){
  const preferences = {
    min_margin: parseFloat(document.getElementById('pref-min-margin').value) || 0,
    max_budget: parseFloat(document.getElementById('pref-max-budget').value) || 0,
  };
  await jpost('/api/onboarding/complete', {preferences, vision: window._visionStatus || {}});
  sdToast('Seuils enregistrés');
}
async function sdSaveVisionSettings(){
  const vision = {
    enabled: document.getElementById('vision-enabled').value === 'true',
    budget_eur_monthly: parseFloat(document.getElementById('vision-budget').value) || 0,
  };
  await jpost('/api/onboarding/complete', {preferences: {}, vision});
  sdToast('Réglages vision enregistrés');
  sdLoadSettingsPage();
}
async function sdRestartOnboarding(){
  await jpost('/api/onboarding/reset', {});
  obShow();
}

// ═══════════════ ONBOARDING ═══════════════
let obStarter = null;
function obShow(){
  document.getElementById('ob-overlay').classList.remove('hidden');
  obGoto(1);
}
function obGoto(step){
  for(let i=1;i<=5;i++){
    document.getElementById('ob-step-'+i).classList.toggle('hidden', i!==step);
    document.getElementById('ob-step-dot-'+i).classList.toggle('done', i<=step);
  }
}
function obSelectStarter(kind, el){
  obStarter = kind;
  document.querySelectorAll('.ob-choice').forEach(c=>c.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('ob-import-field').classList.toggle('hidden', kind!=='import');
}
async function obFinish(){
  const btn = document.getElementById('ob-finish-btn');
  const preferences = {
    sources: {
      lbc: document.getElementById('ob-src-lbc').checked,
      vinted: document.getElementById('ob-src-vinted').checked,
      ebay: document.getElementById('ob-src-ebay').checked,
      facebook: document.getElementById('ob-src-facebook').checked,
    },
    categories: document.getElementById('ob-categories').value.trim(),
    min_margin: parseFloat(document.getElementById('ob-min-margin').value) || 0,
    max_budget: parseFloat(document.getElementById('ob-max-budget').value) || 0,
  };
  const vision = {
    enabled: document.getElementById('ob-vision-enabled').value === 'true',
    budget_eur_monthly: parseFloat(document.getElementById('ob-vision-budget').value) || 0,
  };
  const payload = {preferences, vision, starter_data: obStarter};
  if(obStarter === 'import'){
    try{ payload.import_payload = JSON.parse(document.getElementById('ob-import-json').value || '{}'); }
    catch(e){ sdToast("Le JSON d'import est invalide", true); return; }
  }
  try{ await withLoading(btn, ()=>jpost('/api/onboarding/complete', payload)); }
  catch(e){ return; }
  obGoto(5);
}
function obLand(){
  document.getElementById('ob-overlay').classList.add('hidden');
  document.querySelector('.sd-nav-btn[data-page="scanner"]').click();
  refresh();
}

boot();
</script>
</div><!-- /sd-main -->
</div><!-- /sd-shell -->
</body>
</html>'''


if __name__ == "__main__":
    main()
