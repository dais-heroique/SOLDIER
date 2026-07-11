"""
mcp_server.py — Serveur MCP pour PC Flip Sniper
═══════════════════════════════════════════════════════════════════════
Expose les données du sniper (deals détectés, catalogue de référence,
historique de prix, rapports perf/prix) comme outils MCP, pour que
Claude Code puisse trier/filtrer/analyser les annonces lui-même —
en plus (pas à la place) du filtre local déjà intégré dans app.py.

Ne nécessite PAS que l'app Flask (app.py) soit lancée : ce serveur lit
directement les fichiers JSON produits par le scan (deals_found.json,
price_history.json) et les modules de référence (market_db, perf_db,
scoring, listing_filter). Lance-le, ou laisse Claude Code le lancer,
indépendamment de app.py.

── Installation ──
    pip install mcp

── Enregistrement dans Claude Code ──
    claude mcp add pc-sniper -- python3 /chemin/vers/mcp_server.py

    (adapte le chemin vers ton dossier PC-Sniper ; utilise le python du
    venv si besoin: /chemin/vers/venv/bin/python3 /chemin/vers/mcp_server.py)

Une fois ajouté, dans Claude Code tu peux demander par exemple :
  "Liste les deals GPU avec un score global > 80 et confiance > 85"
  "Compare les 3 meilleures RTX 3070 actuellement détectées"
  "Trouve les deals où la confiance d'état est basse mais le prix est très bon"
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from market_db import CATEGORIES, MIN_PRICE
from scoring import full_report
from price_history import PriceHistory

DEALS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deals_found.json")

mcp = FastMCP("pc-sniper")


def _load_deals():
    if os.path.exists(DEALS_LOG):
        try:
            with open(DEALS_LOG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _find_ref(cat, model):
    c = CATEGORIES.get(cat)
    if not c or model not in c["db"]:
        return None, None
    return c["db"][model], c["label"]


@mcp.tool()
def list_deals(category: str = "", source: str = "", min_score: int = 0,
                min_confidence: int = 0, limit: int = 100) -> list:
    """
    Liste les deals actuellement détectés par le scan Leboncoin+Vinted+eBay+Facebook.

    Args:
        category: filtre par catégorie (GPU, CPU, MOBO, RAM, STORAGE, PSU,
                  COOLING, CASE, LAPTOP, CHAIR). Vide = toutes.
        source: "leboncoin", "vinted", "ebay" ou "facebook". Vide = toutes.
        min_score: score global minimum (0-100, voir champ report.global_score)
        min_confidence: confiance d'état minimum (0-100, voir champ confidence)
        limit: nombre max de résultats (défaut 100)

    Retourne une liste de deals avec: model, category, subject, price, fair,
    margin, tier, source, url, location, confidence, condition, report
    (verdict perf/prix + revendabilité), found_at.
    """
    deals = _load_deals()
    out = []
    for d in deals:
        if category and d.get("category") != category:
            continue
        if source and d.get("source") != source:
            continue
        score = d.get("report", {}).get("global_score", 0)
        if score < min_score:
            continue
        conf = d.get("confidence")
        if conf is not None and conf < min_confidence:
            continue
        out.append(d)
    out.sort(key=lambda d: d.get("report", {}).get("global_score", 0), reverse=True)
    return out[:limit]


@mcp.tool()
def search_deals(query: str, limit: int = 50) -> list:
    """
    Cherche des deals dont le modèle ou le titre d'annonce contient le texte donné.

    Args:
        query: texte à chercher (ex: "RTX 3070", "5600X", "B550")
        limit: nombre max de résultats
    """
    q = query.lower()
    deals = _load_deals()
    out = [d for d in deals
           if q in (d.get("model", "") + " " + d.get("subject", "")).lower()]
    out.sort(key=lambda d: d.get("report", {}).get("global_score", 0), reverse=True)
    return out[:limit]


@mcp.tool()
def get_catalog(category: str = "") -> list:
    """
    Retourne le catalogue de référence (255 modèles, prix fair/good/steal).

    Args:
        category: filtre par catégorie. Vide = toutes.
    """
    out = []
    for ck, c in CATEGORIES.items():
        if category and ck != category:
            continue
        for model, ref in c["db"].items():
            out.append({"category": ck, "catLabel": c["label"], "model": model,
                        "fair": ref["fair"], "good": ref["good"], "steal": ref["steal"],
                        "min_price_floor": MIN_PRICE.get(ck)})
    return out


@mcp.tool()
def get_model_report(category: str, model: str, price: int = 0) -> dict:
    """
    Calcule le rapport complet perf/prix + revendabilité + jouabilité AAA 2026
    pour un modèle précis, à un prix donné (ou au prix "fair" si non précisé).

    Args:
        category: catégorie du composant (ex: "GPU", "CPU")
        model: nom exact du modèle (ex: "RTX 3070", "Ryzen 5 5600X")
        price: prix à évaluer en euros (0 = utilise le prix "fair" de référence)
    """
    ref, cat_label = _find_ref(category, model)
    if not ref:
        return {"error": f"Modèle introuvable: {category}/{model}"}
    p = price or ref["fair"]
    report = full_report(category, model, p, ref, True)
    return {"category": category, "catLabel": cat_label, "model": model,
            "price": p, "ref": ref, "report": report}


@mcp.tool()
def get_price_history(category: str, model: str) -> dict:
    """
    Retourne l'historique journalier des prix observés (min/médian/max/n par jour)
    pour un modèle, ainsi que des stats globales. Utile pour comparer un prix
    d'annonce à l'évolution réelle du marché.

    Args:
        category: catégorie du composant
        model: nom exact du modèle
    """
    ph = PriceHistory()
    series = ph.series(category, model)
    stats = ph.stats(category, model)
    return {"category": category, "model": model, "daily_series": series, "stats": stats}


@mcp.tool()
def compare_models(category: str, models: list) -> list:
    """
    Compare plusieurs modèles d'une même catégorie côte à côte : prix de
    référence, rapport perf/prix, jouabilité AAA 2026, score de revendabilité.
    Utile pour "quel GPU choisir entre X, Y et Z".

    Args:
        category: catégorie (ex: "GPU")
        models: liste de noms de modèles exacts à comparer
    """
    out = []
    for model in models:
        ref, cat_label = _find_ref(category, model)
        if not ref:
            out.append({"model": model, "error": "introuvable"})
            continue
        report = full_report(category, model, ref["fair"], ref, True)
        out.append({"model": model, "ref": ref, "report": report})
    return out


@mcp.tool()
def get_stats() -> dict:
    """
    Statistiques globales du scan en cours: nombre de deals, marge cumulée,
    répartition par source (Leboncoin/Vinted) et par catégorie.
    """
    deals = _load_deals()
    n_lbc = sum(1 for d in deals if d.get("source") == "leboncoin")
    n_vinted = sum(1 for d in deals if d.get("source") == "vinted")
    n_steal = sum(1 for d in deals if d.get("tier") == "steal")
    margin = sum(d.get("margin", 0) for d in deals if d.get("margin", 0) > 0)
    by_cat = {}
    for d in deals:
        c = d.get("category", "?")
        by_cat[c] = by_cat.get(c, 0) + 1
    return {"total_deals": len(deals), "leboncoin": n_lbc, "vinted": n_vinted,
            "affaires_en_or": n_steal, "marge_cumulee": margin, "par_categorie": by_cat}


# ── Archive permanente (deals_archive.jsonl) ──────────────────────────
# Contrairement à list_deals() (dashboard actif, nettoyé régulièrement),
# ces outils lisent l'archive JAMAIS purgée — tout ce qui a été trouvé
# depuis le premier lancement, pour analyse historique.

ARCHIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deals_archive.jsonl")
RAW_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_observations.jsonl")


def _iter_jsonl(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


@mcp.tool()
def get_archive_stats() -> dict:
    """
    Statistiques sur l'ARCHIVE PERMANENTE (tous les deals jamais trouvés,
    y compris ceux retirés depuis longtemps du dashboard actif). Donne le
    nombre total, la période couverte, et la répartition par catégorie.
    """
    n = 0
    by_cat = {}
    first_ts, last_ts = None, None
    for d in _iter_jsonl(ARCHIVE_FILE):
        n += 1
        c = d.get("category", "?")
        by_cat[c] = by_cat.get(c, 0) + 1
        ts = d.get("ts")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
    return {"total_deals_archives": n, "par_categorie": by_cat,
            "premiere_observation_ts": first_ts, "derniere_observation_ts": last_ts}


@mcp.tool()
def search_archive(query: str = "", category: str = "", model: str = "",
                    min_score: int = 0, limit: int = 200) -> list:
    """
    Cherche dans l'ARCHIVE PERMANENTE (tous les deals jamais détectés,
    contrairement à list_deals() qui ne montre que le dashboard actif
    nettoyé). Utile pour de l'analyse historique: "combien de RTX 3070
    sous 200€ ont été vues depuis le début", tendances, etc.

    Args:
        query: texte à chercher dans le modèle ou le titre d'annonce
        category: filtre par catégorie exacte
        model: filtre par modèle exact
        min_score: score global minimum
        limit: nombre max de résultats (les plus récents en premier)
    """
    q = query.lower()
    out = []
    for d in _iter_jsonl(ARCHIVE_FILE):
        if category and d.get("category") != category:
            continue
        if model and d.get("model") != model:
            continue
        if q and q not in (d.get("model", "") + " " + d.get("subject", "")).lower():
            continue
        if d.get("report", {}).get("global_score", 0) < min_score:
            continue
        out.append(d)
    out.sort(key=lambda d: d.get("ts", 0), reverse=True)
    return out[:limit]


@mcp.tool()
def get_raw_price_log(category: str, model: str, limit: int = 500) -> list:
    """
    Retourne le LOG BRUT PERMANENT des prix observés pour un modèle — chaque
    prix individuel avec son horodatage exact (pas juste l'agrégat quotidien
    de get_price_history). Utile pour une analyse fine (volatilité intra-
    journée, tendance récente précise...).

    Args:
        category: catégorie du composant
        model: nom exact du modèle
        limit: nombre max d'observations renvoyées (les plus récentes en premier)
    """
    out = [d for d in _iter_jsonl(RAW_LOG_FILE)
           if d.get("cat") == category and d.get("model") == model]
    out.sort(key=lambda d: d.get("t", ""), reverse=True)
    return out[:limit]


if __name__ == "__main__":
    mcp.run(transport="stdio")
