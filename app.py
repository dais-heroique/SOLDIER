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
SCAN_INTERVAL = 90
LIMIT_PER_QUERY = 12
DELAY_MIN, DELAY_MAX = 1.4, 2.8
VINTED_DELAY = (1.0, 2.2)
EBAY_DELAY = (0.3, 0.8)   # API officielle, pas de scraping -> pas besoin d'être aussi prudent

DEALS_LOG = "deals_found.json"
SEEN_FILE = "seen_ads.json"
ARCHIVE_FILE = "deals_archive.jsonl"   # archive PERMANENTE, jamais purgée (JSON Lines: 1 deal par ligne)

# ── Nettoyage du dashboard ACTIF (pas de l'archive!) ────────────────────
# Le dashboard reste volontairement curé (une annonce de 3 mois est
# probablement vendue -> l'afficher comme "live" serait du bruit inutile).
# Avec beaucoup d'espace disque disponible, on peut se permettre de garder
# les deals actifs plus longtemps avant de les considérer expirés, ET on
# garde TOUT pour toujours dans l'archive séparée (voir ARCHIVE_FILE).
DEAL_MAX_AGE_DAYS = 45     # deals affichés dans le dashboard "live"
DEAL_MAX_COUNT = 3000      # cap du dashboard actif (au-delà: meilleurs scores + plus récents)
SEEN_TTL_DAYS = 90         # mémoire anti-doublon plus longue, pas de souci d'espace

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

    d = {"category": cat, "model": model, "subject": ad["subject"], "price": ad["price"],
         "fair": ref["fair"], "good": ref["good"], "steal": ref["steal"], "margin": margin,
         "tier": tier, "functional": functional, "flags": flags, "url": ad["url"],
         "image": ad.get("image",""), "location": ad.get("location",""), "source": source,
         "ships": ad.get("ships"),
         "found_at": datetime.fromtimestamp(display_ts).strftime("%d/%m %H:%M"),
         "date_is_real": date_is_real,
         "posted_ts": display_ts,     # pour le tri "plus récent" (date réelle si dispo)
         "ts": scan_ts,               # heure de SCAN — sert uniquement au nettoyage/archivage
         "report": report}
    if listing:
        d["confidence"] = listing["confidence"]
        d["condition"] = listing["condition"]
    return d


# ─────────────────────── SCRAPERS ───────────────────────
def scan_lbc(client, cat, model, ref, seen, observed, dup_tracker=None):
    deals = []
    is_psu = (cat == "PSU")
    floor = MIN_PRICE.get(cat, 5)
    tokens = TOKEN_CACHE.get((cat, model), [])
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
                if not model_matches(subject, desc, tokens):
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
                    deals.append(make_deal(cat,model,ref,norm,tier,func,margin,"leboncoin",listing=listing))
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
    tokens = TOKEN_CACHE.get((cat, model), [])
    for q in ref["queries"]:
        try:
            items = vc.search(search_text=q, price_to=max(ref["fair"], ref["good"]))
            for ad in items:
                url=ad["url"]; subject=ad["subject"]; desc=ad.get("description",""); price=ad["price"]
                if not model_matches(subject, desc, tokens):
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
                    deals.append(make_deal(cat,model,ref,ad,tier,func,margin,"vinted",listing=listing))
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
    tokens = TOKEN_CACHE.get((cat, model), [])
    for q in ref["queries"]:
        try:
            items = ec.search(q, price_to=max(ref["fair"], ref["good"]), price_from=floor)
            for ad in items:
                url=ad["url"]; subject=ad["subject"]; desc=ad.get("description",""); price=ad["price"]
                if not model_matches(subject, desc, tokens):
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
                    deals.append(make_deal(cat,model,ref,ad,tier,func,margin,"ebay",listing=listing))
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
    tokens = TOKEN_CACHE.get((cat, model), [])

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
                if not model_matches(subject, desc, tokens):
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
                    deals.append(make_deal(cat,model,ref,ad,tier,func,margin,"facebook",listing=listing))
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
    action = request.json.get("action")
    if action == "pause":
        STATE["running"] = False
    elif action == "resume":
        STATE["running"] = True
    return jsonify({"running": STATE["running"]})


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


def main():
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
<title>🎯 PC Flip Sniper</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@400;600;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#080c10;--s1:#0e1318;--s2:#141b22;--s3:#1a232c;--bd:#1e2830;--tx:#dde6ee;--mu:#5a7080;--accent:#00ff88;--gold:#ffd700;--steal:#ff3e6c;--lbc:#ff6b35;--vinted:#09b1ba;--ebay:#e53238;--facebook:#1877f2}
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
.card.flash{animation:flash 1.2s}
@keyframes flash{0%{box-shadow:0 0 0 2px var(--gold)}100%{box-shadow:none}}
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
.verdict.excellent{background:var(--gold);color:#000}
.verdict.good{background:#00ff8822;color:var(--accent)}
.verdict.ok{background:#ffd16622;color:var(--gold)}
.verdict.meh{background:#5a708022;color:var(--mu)}
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
</style>
</head>
<body>
<header>
  <div class="logo">🎯 PC<span>Sniper</span><small id="sub">…</small></div>
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
    <button class="ctrl" id="settings-btn" onclick="openSettings()">⚙️</button>
    <button class="ctrl" id="toggle" onclick="toggleScan()">⏸</button>
  </div>
</header>

<div class="scanbar">
  <span class="dot" id="dot"></span>
  <span id="scanstatus" data-i18n="scanning_init">Initialisation…</span>
  <span class="pbar"><span class="pfill" id="pfill" style="width:0%"></span></span>
</div>

<div class="searchbar">
  <input id="search" data-i18n-ph="search_placeholder" placeholder="🔎 Cherche un composant (ex: RTX 3060, Ryzen 5600, DDR4 32, B550…)" oninput="onSearch()">
</div>
<div class="tabs">
  <button class="tab active" onclick="switchTab('deals',this)"><span data-i18n="tab_deals">🔥 Deals en direct</span></button>
  <button class="tab" onclick="switchTab('eval',this)"><span data-i18n="tab_eval">📊 Évaluateur de prix</span></button>
</div>

<div id="tab-deals">
  <div class="filters" id="deals-platform-filters">
    <span class="chip on" id="chip-leboncoin" onclick="togglePlatform('leboncoin',this)"><input type="checkbox" checked readonly>🟧 Leboncoin</span>
    <span class="chip on" id="chip-vinted" onclick="togglePlatform('vinted',this)"><input type="checkbox" checked readonly>🟦 Vinted</span>
    <span class="chip on" id="chip-ebay" onclick="togglePlatform('ebay',this)"><input type="checkbox" checked readonly>🟥 eBay</span>
    <span class="chip on" id="chip-facebook" onclick="togglePlatform('facebook',this)"><input type="checkbox" checked readonly>🟦 Facebook</span>
    <span class="chip" id="chip-steal" onclick="toggleStealOnly(this)"><input type="checkbox" readonly>💎 <span data-i18n="filter_steal_only">Affaires en or uniquement</span></span>
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
    <span class="chip on" id="chip-v-excellent" onclick="toggleVerdict('excellent',this)"><input type="checkbox" checked readonly>💎</span>
    <span class="chip on" id="chip-v-good" onclick="toggleVerdict('good',this)"><input type="checkbox" checked readonly>✅</span>
    <span class="chip on" id="chip-v-ok" onclick="toggleVerdict('ok',this)"><input type="checkbox" checked readonly>🟡</span>
    <span class="chip on" id="chip-v-meh" onclick="toggleVerdict('meh',this)"><input type="checkbox" checked readonly>🟠</span>
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
  <main><div class="bar"><span class="sub" id="deals-sub"></span></div><div class="grid" id="deals-grid"></div></main>
</div>

<div id="tab-eval" class="hidden">
  <div class="filters" id="cat-filters">
    <button class="fbtn active" onclick="filterCat('all',this)" data-i18n="filter_all_categories">Toutes catégories</button>
  </div>
  <main><div class="bar"><span class="sub" data-i18n="eval_hint">Clique un modèle pour voir l'évaluation + le graphique d'évolution du marché</span></div>
  <div class="eval-grid" id="eval-grid"></div></main>
</div>

<footer>
  <span data-i18n="footer_line1">🎯 PC Flip Sniper · app locale Flask · Leboncoin + Vinted + eBay</span><br>
  <span data-i18n="footer_line2">💎 sous "steal" · 🟢 sous "good" · annonces HS/arnaque jamais alertées</span>
</footer>

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
      <div class="modal-title" data-i18n="settings_title">⚙️ Paramètres</div>
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
  fr: {GPU:"🎮 Cartes graphiques", CPU:"🧠 Processeurs", MOBO:"🔌 Cartes mères", RAM:"💾 Mémoire RAM",
       STORAGE:"💿 Stockage", PSU:"⚡ Alimentations", COOLING:"❄️ Refroidissement", CASE:"🖥️ Boîtiers",
       MONITOR:"🖵 Écrans", KEYBOARD:"⌨️ Claviers", MOUSE:"🖱️ Souris", HEADSET:"🎧 Casques",
       LAPTOP:"💻 PC portables", CHAIR:"🪑 Sièges gaming"},
  en: {GPU:"🎮 Graphics cards", CPU:"🧠 Processors", MOBO:"🔌 Motherboards", RAM:"💾 RAM memory",
       STORAGE:"💿 Storage", PSU:"⚡ Power supplies", COOLING:"❄️ Cooling", CASE:"🖥️ Cases",
       MONITOR:"🖵 Monitors", KEYBOARD:"⌨️ Keyboards", MOUSE:"🖱️ Mice", HEADSET:"🎧 Headsets",
       LAPTOP:"💻 Laptops", CHAIR:"🪑 Gaming chairs"},
};
function catLabel(code){ return (CAT_LABELS[LANG] && CAT_LABELS[LANG][code]) || code; }

const I18N = {
fr: {
  stat_deals:"Deals", stat_steals:"Affaires or", stat_margin:"Marge cumul.", stat_cycles:"Cycles",
  scanning_init:"Initialisation…",
  scanning_status:"Scan: {cat} · {model}",
  scanning_paused:"⏸ En pause",
  scanning_done:"Cycle {cycle} terminé · {new} nouveau(x) deal(s) · prochaine passe imminente",
  search_placeholder:"🔎 Cherche un composant (ex: RTX 3060, Ryzen 5600, DDR4 32, B550…)",
  tab_deals:"🔥 Deals en direct", tab_eval:"📊 Évaluateur de prix",
  filter_all_categories:"Toutes catégories",
  filter_steal_only:"Affaires en or uniquement",
  label_price_min:"Prix min", label_price_max:"Prix max",
  label_delivery:"Livraison", delivery_all:"Peu importe", delivery_ships:"Livraison possible", delivery_pickup:"Remise en main propre",
  label_min_score:"Score min", label_min_confidence:"Confiance min", filter_price_reset:"Réinitialiser",
  label_sort:"Trier par", sort_margin_desc:"Marge (décroissant)", sort_price_asc:"Prix (croissant)",
  sort_price_desc:"Prix (décroissant)", sort_score_desc:"Score global (décroissant)", sort_recent:"Plus récent",
  deals_sub:"{shown}/{total} deal(s) affiché(s) · live",
  empty_deals:"Aucun deal ne correspond à ces filtres.",
  empty_deals_none:"Aucun deal pour le moment. Le scan tourne…",
  eval_hint:"Clique un modèle pour voir l'évaluation + le graphique d'évolution du marché",
  footer_line1:"🎯 PC Flip Sniper · app locale Flask · Leboncoin + Vinted + eBay",
  footer_line2:"💎 sous \"steal\" · 🟢 sous \"good\" · annonces HS/arnaque jamais alertées",
  open_listing:"Ouvrir l'annonce ↗", open_hint:"🔍 fiche complète",
  modal_sim_label:"🎚️ Simule un prix d'achat — vois le verdict en direct :",
  modal_history_title:"📈 Historique des prix vus (marché)",
  modal_listings_title:"📦 Annonces actuelles ({count})",
  modal_no_history:"Pas encore d'historique pour ce modèle.<br>Le graphique se construit au fil des cycles de scan.",
  modal_no_listings:"Aucune annonce actuellement détectée pour ce modèle précis. Le scan continue en fond.",
  stat_min:"Min vu", stat_med:"Médian", stat_max:"Max vu", stat_obs:"Observations",
  settings_title:"⚙️ Paramètres", settings_lang_label:"Langue de l'interface",
  settings_country_label:"Pays (source des annonces)", settings_platforms_label:"Plateformes actives",
  settings_location_label:"Ta ville (pour Facebook Marketplace)",
  settings_location_ph:"ex: Annecy, Chambéry, 74000...",
  settings_location_note:"Facebook cherche uniquement autour d'un point précis (pas dans tout le pays) — sans ta ville, il cherche par défaut autour de la capitale, ce qui peut être très loin de chez toi.",
  settings_radius_label:"Rayon de recherche",
  settings_saved_note:"Leboncoin n'est disponible qu'en France. Un changement redémarre le scan automatiquement (sans relancer l'app) avec la nouvelle configuration.",
  settings_save:"Enregistrer", settings_saved:"✅ Enregistré, le scan redémarre avec la nouvelle config…",
  verdict_excellent:"💎 À SAISIR", verdict_good:"✅ BON DEAL", verdict_ok:"🟡 CORRECT", verdict_meh:"🟠 MOYEN / À NÉGOCIER",
  badge_steal:"💎 AFFAIRE EN OR", badge_good:"🟢 BON DEAL",
  sub_deal:"deal", sub_resale:"revente", sub_perfprice:"perf/€", sub_demand:"demande",
  resell_line:"💰 Revente: marge {margin}€ ({marginpct}%) · demande {demand}/100 · fraîcheur {freshness}/100",
  conf_hi:"✓ bon état", conf_mid:"? à vérifier", conf_lo:"⚠ peu d'info",
  src_market:"🟢 marché réel", src_pcpp:"🟡 PCPartPicker", src_estimate:"⚪ estimation",
  note_market:"basé sur {n} annonces observées (min {min}€ / max {max}€)",
  note_pcpp:"neuf {new_price}€ (PCPartPicker, {date}) × décote âge",
  note_estimate:"estimation de départ, pas encore confirmée par le marché ou PCPartPicker",
  ship_yes:"🚚 Livraison", ship_no:"🤝 Main propre",
  flag_accessory:"🚫 Accessoire détecté ({type}) — pas le composant lui-même",
  flag_bad_condition:"🔴 État probablement HS/défectueux (« {word} »)",
  flag_good_condition:"✅ Bon état confirmé (« {word} »)",
  flag_unknown_short:"ℹ️ État non précisé + description très courte — demande photos/test avant achat",
  flag_unknown:"ℹ️ État non explicitement confirmé — à vérifier avant achat",
  flag_scam:"🚨 Pattern arnaque (contact hors plateforme)",
  flag_scam_high:"🚨 Risque d'arnaque élevé (score {score}/100) — annonce écartée",
  flag_scam_moderate:"⚠️ Signaux suspects détectés (score {score}/100) — grande prudence recommandée",
  acc_type_box:"carton/boîte seule", acc_type_cable:"câble/adaptateur",
  acc_type_water:"waterblock/refroidissement seul", acc_type_support:"support/accessoire",
  acc_type_search:"recherche/achat",
},
en: {
  stat_deals:"Deals", stat_steals:"Steal deals", stat_margin:"Total margin", stat_cycles:"Cycles",
  scanning_init:"Initializing…",
  scanning_status:"Scanning: {cat} · {model}",
  scanning_paused:"⏸ Paused",
  scanning_done:"Cycle {cycle} done · {new} new deal(s) · next pass coming up",
  search_placeholder:"🔎 Search a component (e.g. RTX 3060, Ryzen 5600, DDR4 32, B550…)",
  tab_deals:"🔥 Live deals", tab_eval:"📊 Price evaluator",
  filter_all_categories:"All categories",
  filter_steal_only:"Steal deals only",
  label_price_min:"Min price", label_price_max:"Max price",
  label_delivery:"Shipping", delivery_all:"Any", delivery_ships:"Shipping available", delivery_pickup:"Local pickup only",
  label_min_score:"Min score", label_min_confidence:"Min confidence", filter_price_reset:"Reset",
  label_sort:"Sort by", sort_margin_desc:"Margin (highest first)", sort_price_asc:"Price (lowest first)",
  sort_price_desc:"Price (highest first)", sort_score_desc:"Overall score (highest first)", sort_recent:"Most recent",
  deals_sub:"{shown}/{total} deal(s) shown · live",
  empty_deals:"No deal matches these filters.",
  empty_deals_none:"No deals yet. Scan in progress…",
  eval_hint:"Click a model to see its full evaluation + market price chart",
  footer_line1:"🎯 PC Flip Sniper · local Flask app · Leboncoin + Vinted + eBay",
  footer_line2:"💎 below \"steal\" · 🟢 below \"good\" · broken/scam listings never alerted",
  open_listing:"Open listing ↗", open_hint:"🔍 full sheet",
  modal_sim_label:"🎚️ Simulate a purchase price — see the verdict live:",
  modal_history_title:"📈 Observed price history (market)",
  modal_listings_title:"📦 Current listings ({count})",
  modal_no_history:"No price history yet for this model.<br>The chart builds up over scan cycles.",
  modal_no_listings:"No listing currently detected for this exact model. The scan keeps running in the background.",
  stat_min:"Min seen", stat_med:"Median", stat_max:"Max seen", stat_obs:"Observations",
  settings_title:"⚙️ Settings", settings_lang_label:"Interface language",
  settings_country_label:"Country (listings source)", settings_platforms_label:"Active platforms",
  settings_location_label:"Your city (for Facebook Marketplace)",
  settings_location_ph:"e.g. Annecy, Denver, 80202...",
  settings_location_note:"Facebook only searches around a specific point (not the whole country) — without your city, it defaults to searching around the capital, which could be very far from you.",
  settings_radius_label:"Search radius",
  settings_saved_note:"Leboncoin is only available in France. Any change automatically restarts the scan (without relaunching the app) with the new configuration.",
  settings_save:"Save", settings_saved:"✅ Saved, the scan is restarting with the new config…",
  verdict_excellent:"💎 GRAB IT", verdict_good:"✅ GOOD DEAL", verdict_ok:"🟡 FAIR", verdict_meh:"🟠 MEDIOCRE / NEGOTIATE",
  badge_steal:"💎 STEAL DEAL", badge_good:"🟢 GOOD DEAL",
  sub_deal:"deal", sub_resale:"resale", sub_perfprice:"perf/€", sub_demand:"demand",
  resell_line:"💰 Resale: margin {margin}€ ({marginpct}%) · demand {demand}/100 · freshness {freshness}/100",
  conf_hi:"✓ good condition", conf_mid:"? to verify", conf_lo:"⚠ little info",
  src_market:"🟢 real market", src_pcpp:"🟡 PCPartPicker", src_estimate:"⚪ estimate",
  note_market:"based on {n} observed listings (min {min}€ / max {max}€)",
  note_pcpp:"new {new_price}€ (PCPartPicker, {date}) × age discount",
  note_estimate:"starting estimate, not yet confirmed by the market or PCPartPicker",
  ship_yes:"🚚 Ships", ship_no:"🤝 Pickup only",
  flag_accessory:"🚫 Accessory detected ({type}) — not the component itself",
  flag_bad_condition:"🔴 Likely broken/defective (\"{word}\")",
  flag_good_condition:"✅ Good condition confirmed (\"{word}\")",
  flag_unknown_short:"ℹ️ Condition not stated + very short description — ask for photos/a test before buying",
  flag_unknown:"ℹ️ Condition not explicitly confirmed — verify before buying",
  flag_scam:"🚨 Scam pattern (off-platform contact)",
  flag_scam_high:"🚨 High scam risk (score {score}/100) — listing discarded",
  flag_scam_moderate:"⚠️ Suspicious signals detected (score {score}/100) — extra caution advised",
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

async function jget(u){const r=await fetch(u);return r.json();}
async function jpost(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json();}

async function boot(){
  const savedLang = localStorage.getItem('sniper_lang');
  if(savedLang) LANG = savedLang;

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
  document.getElementById('s-margin').textContent=margin+'€';
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
  document.getElementById('toggle').textContent=st.running?'⏸':'▶';
  document.getElementById('toggle').className='ctrl'+(st.running?'':' pause');
  renderDeals();
}

// ═══════════════ HELPERS DE RENDU ═══════════════
function confBadge(d){
  if(d.confidence==null) return '';
  const c=d.confidence;
  const cls=c>=85?'conf-hi':c>=55?'conf-mid':'conf-lo';
  const lab=c>=85?t('conf_hi'):c>=55?t('conf_mid'):t('conf_lo');
  return `<span class="conf-badge ${cls}" title="${c}/100">${lab}</span>`;
}
function shipTag(d){
  if(d.ships===true) return `<span class="ship-tag">${t('ship_yes')}</span>`;
  if(d.ships===false) return `<span class="ship-tag">${t('ship_no')}</span>`;
  return '';
}
function scoreColor(v){return v>=80?'#ffd700':v>=65?'#00ff88':v>=50?'#ffd166':'#ff3e6c';}
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

// ═══════════════ CARTES DEALS ═══════════════
function dealCard(d){
  const c=CATEGORIES[d.category]||{color:'#888'};
  const isSteal=d.tier==='steal';
  const bcls=isSteal?'steal':'good';
  const badge=isSteal?t('badge_steal'):t('badge_good');
  const SRC_MAP={leboncoin:'Leboncoin',vinted:'Vinted',ebay:'eBay',facebook:'Facebook'};
  const srcLabel=SRC_MAP[d.source]||d.source;
  const scls=d.source==='leboncoin'?'lbc':d.source;
  const img=d.image?`<img src="${d.image}" loading="lazy" onerror="this.parentElement.innerHTML='📦'">`:'📦';
  const func=d.functional?'<span class="func">✓</span>':'';
  const flags=(d.flags||[]).map(f=>`<div class="flag">${renderFlag(f)}</div>`).join('');
  const isNew=!knownUrls.has(d.url);
  return `<div class="card ${bcls} ${isNew&&!firstLoad?'flash':''}" data-source="${d.source}" data-tier="${d.tier}" data-ships="${d.ships}" data-model="${(d.model||'').toLowerCase()}" onclick="openDetail('${d.category}','${escJs(d.model)}')">
    <div class="card-img">${img}<span class="src-tag ${scls}">${srcLabel}</span>${confBadge(d)}${shipTag(d)}</div>
    <div class="card-body"><span class="badge ${bcls}">${badge}</span>
      <div class="model">${d.model} ${func}</div>
      <div class="subject">${(d.subject||'').slice(0,60)}</div>
      <div class="price-row"><span class="price" style="color:${c.color}">${d.price}€</span><span class="margin">+${d.margin}€</span></div>
      <div class="ref">${d.fair}€ · &lt;${d.good}€ · &lt;${d.steal}€</div>
      <div class="loc">📍 ${d.location||'—'} · ${d.date_is_real?'📅':'🕐~'} ${d.found_at||''}</div>
      <div class="flags">${flags}</div>${reportHTML(d.report)}
      <div class="card-actions">
        <a class="lst-open" href="${d.url}" target="_blank" onclick="event.stopPropagation()">${t('open_listing')}</a>
        <span class="open-hint">${t('open_hint')}</span>
      </div>
    </div></div>`;
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
  grid.innerHTML=filtered.length?filtered.map(dealCard).join(''):`<p class="empty">${DEALS.length?t('empty_deals'):t('empty_deals_none')}</p>`;
  document.getElementById('deals-sub').textContent=t('deals_sub',{shown:filtered.length,total:DEALS.length});
}

// ═══════════════ ÉVALUATEUR ═══════════════
function evalCard(it){
  const maxv=it.fair*1.1, pct=v=>Math.max(6,Math.round(v/maxv*100));
  return `<div class="eval-card" data-cat="${it.cat}" data-model="${it.model.toLowerCase()}" onclick="openDetail('${it.cat}','${escJs(it.model)}')">
    <div class="eval-head"><div><div class="eval-model">${it.model} ${srcPill(it.source)}</div><div class="eval-cat">${catLabel(it.cat)}</div></div><div class="eval-fair">${it.fair}€</div></div>
    <div class="eval-bars">
      <div class="eval-bar"><span class="lab">or</span><span class="track"><span class="fill" style="width:${pct(it.steal)}%;background:#ff3e6c"></span></span><span class="val">${it.steal}€</span></div>
      <div class="eval-bar"><span class="lab">deal</span><span class="track"><span class="fill" style="width:${pct(it.good)}%;background:#00ff88"></span></span><span class="val">${it.good}€</span></div>
      <div class="eval-bar"><span class="lab">${LANG==='en'?'fair':'juste'}</span><span class="track"><span class="fill" style="width:${pct(it.fair)}%;background:#ffd700"></span></span><span class="val">${it.fair}€</span></div>
    </div>
    <div class="open-hint" style="padding:0 1rem .8rem">🔍 ${t('eval_hint')}</div>
  </div>`;
}
function renderEval(){document.getElementById('eval-grid').innerHTML=CATALOG.map(evalCard).join('');applyEvalFilters();}
function filterCat(c,btn){document.querySelectorAll('#tab-eval .fbtn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');window._evalCat=c;applyEvalFilters();}
function applyEvalFilters(){
  const q=document.getElementById('search').value.toLowerCase();
  const cf=window._evalCat||'all';
  document.querySelectorAll('#eval-grid .eval-card').forEach(c=>{
    let ok=true;
    if(cf!=='all')ok=c.dataset.cat===cf;
    if(ok&&q)ok=c.dataset.model.includes(q);
    c.style.display=ok?'block':'none';
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
      <span class="lst-subject">${(l.subject||'').slice(0,55)}</span>
      <span class="lst-conf">${l.confidence!=null?l.confidence+'%':''}</span>
      <span class="lst-date">${l.date_is_real?'📅':'🕐~'} ${l.found_at||''}</span>
      <span class="lst-price">${l.price}€</span>
      <a class="lst-open" href="${l.url}" target="_blank" onclick="event.stopPropagation()">${t('open_listing')}</a>
    </div>`;
  }).join('') : `<div class="no-hist">${t('modal_no_listings')}</div>`;

  body.innerHTML = `
    <div class="price-source-line">${srcPill(ref.source)}<span>${priceNote(ref)}</span></div>
    <div class="eval-bars" style="margin-bottom:1rem">
      <div class="eval-bar"><span class="lab">or</span><span class="track"><span class="fill" style="width:${pct(ref.steal)}%;background:#ff3e6c"></span></span><span class="val">${ref.steal}€</span></div>
      <div class="eval-bar"><span class="lab">deal</span><span class="track"><span class="fill" style="width:${pct(ref.good)}%;background:#00ff88"></span></span><span class="val">${ref.good}€</span></div>
      <div class="eval-bar"><span class="lab">${LANG==='en'?'fair':'juste'}</span><span class="track"><span class="fill" style="width:${pct(ref.fair)}%;background:#ffd700"></span></span><span class="val">${ref.fair}€</span></div>
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
    <div class="sim-out"><span id="modal-rngval">${ref.good}€</span><span class="sim-verdict" id="modal-rngverd">…</span></div>
    <div id="modal-rngrep" style="margin-top:.5rem"></div>`;
  modalSimUpdate(cat, model);

  if(data.history.length){
    const days=data.history.map(h=>h.day), med=data.history.map(h=>h.med), mn=data.history.map(h=>h.min), mx=data.history.map(h=>h.max);
    const gmin=Math.min(...mn), gmax=Math.max(...mx);
    const sm=med.slice().sort((a,b)=>a-b), gmed=sm[Math.floor(sm.length/2)];
    const n=data.history.reduce((s,h)=>s+h.n,0);
    document.getElementById('modal-stats').innerHTML=`
      <div class="es"><div class="esv" style="color:#ff3e6c">${gmin}€</div><div class="esl">${t('stat_min')}</div></div>
      <div class="es"><div class="esv" style="color:#ffd700">${gmed}€</div><div class="esl">${t('stat_med')}</div></div>
      <div class="es"><div class="esv" style="color:#00ff88">${gmax}€</div><div class="esl">${t('stat_max')}</div></div>
      <div class="es"><div class="esv">${n}</div><div class="esl">${t('stat_obs')}</div></div>`;
    if(modalChart){modalChart.destroy();modalChart=null;}
    const ctx=document.getElementById('modal-chart');
    if(ctx){
      modalChart=new Chart(ctx,{type:'line',data:{labels:days,datasets:[
        {label:t('stat_med'),data:med,borderColor:'#ffd700',backgroundColor:'#ffd70022',tension:.3,fill:true,pointRadius:2},
        {label:t('stat_min'),data:mn,borderColor:'#ff3e6c',tension:.3,pointRadius:1,borderDash:[4,4]},
        {label:t('stat_max'),data:mx,borderColor:'#00ff88',tension:.3,pointRadius:1,borderDash:[4,4]}]},
        options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#dde6ee',font:{size:10}}}},
        scales:{x:{ticks:{color:'#5a7080',font:{size:9}},grid:{color:'#1e2830'}},y:{ticks:{color:'#5a7080',font:{size:9},callback:v=>v+'€'},grid:{color:'#1e2830'}}}}});
    }
  }
}
function modalSimUpdate(cat, model){
  const rng=document.getElementById('modal-rng'); if(!rng) return;
  const price=rng.value;
  document.getElementById('modal-rngval').textContent=price+'€';
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

boot();
</script>
</body>
</html>'''


if __name__ == "__main__":
    main()
