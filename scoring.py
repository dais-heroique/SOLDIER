"""
scoring.py — Moteur de scoring PERF/PRIX + REVENDABILITÉ + AAA 2026
═══════════════════════════════════════════════════════════════════════
Produit pour chaque annonce/modèle un rapport clair avec:

  1. PERF/PRIX      : indice de perf ÷ prix, normalisé 0-100
  2. AAA 2026       : verdict jouabilité triple-A (réso + VRAM)
  3. DEAL SCORE     : à quel point le prix est bon vs marché (fair/good/steal)
  4. REVENDABILITÉ  : score multi-critères (5+ paramètres, voir ci-dessous)
  5. VERDICT GLOBAL : note /100 + label (à saisir / bon / correct / à éviter)

── Les 5+ paramètres de la REVENDABILITÉ ──
  a) Demande marché (popularité/liquidité du modèle)
  b) Marge de revente potentielle (fair - prix d'achat)
  c) Ratio de marge (marge / prix, en %)
  d) Fraîcheur techno (âge de la génération — récent = revend mieux)
  e) Pertinence 2026 (jouable AAA aujourd'hui = demande soutenue)
  f) Bonus VRAM (≥12 Go = argument de vente fort en 2026)
  g) Confiance fonctionnelle (annonce indique testé/fonctionnel)
"""

from datetime import datetime
from perf_db import GPU_PERF, CPU_PERF, DEMAND, DEMAND_DEFAULT, demand_for

CURRENT_YEAR = 2026

RES_LABEL = {
    "4K": "🏆 4K Ultra jouable",
    "1440p": "✅ 1440p (QHD) confortable",
    "1080p": "✅ 1080p (Full HD) confortable",
    "1080p-low": "⚠️ 1080p détails réduits seulement",
}
RES_RANK = {"4K": 4, "1440p": 3, "1080p": 2, "1080p-low": 1}


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def get_perf(category, model):
    if category == "GPU":
        return GPU_PERF.get(model)
    if category == "CPU":
        return CPU_PERF.get(model)
    return None


def perf_per_price(category, model, price):
    """Indice perf/prix normalisé 0-100 (relatif au meilleur ratio de la catégorie)."""
    info = get_perf(category, model)
    if not info or price <= 0:
        return None
    raw = info["perf"] / price  # perf par euro
    # Normalisation: on calcule le meilleur ratio possible dans la catégorie
    # à un prix "fair" pour donner une échelle stable.
    db = GPU_PERF if category == "GPU" else CPU_PERF
    # référence: ratio moyen haut = ~0.5 perf/€ pour un très bon deal GPU
    ref = 0.55 if category == "GPU" else 0.9
    return round(clamp(raw / ref * 100), 1)


def aaa_verdict(category, model):
    """Jouabilité AAA 2026."""
    info = get_perf(category, model)
    if not info:
        return None
    if category == "GPU":
        res = info["res"]
        vram = info["vram"]
        label = RES_LABEL.get(res, res)
        # avertissement VRAM 2026
        warn = ""
        if vram <= 4:
            warn = " · 🔴 VRAM ≤4Go: insuffisant pour beaucoup de AAA 2026"
        elif vram <= 6:
            warn = " · 🟠 6Go: limite en textures hautes"
        elif vram == 8:
            warn = " · 🟡 8Go: ok 1080p, juste en 1440p"
        elif vram >= 16:
            warn = " · 🟢 " + str(vram) + "Go: confortable et durable"
        elif vram >= 12:
            warn = " · 🟢 " + str(vram) + "Go: bon pour durer"
        playable = RES_RANK.get(res, 0) >= 2
        return {"res": res, "label": label + warn, "vram": vram,
                "playable_aaa": playable, "rt": info.get("rt", 0)}
    else:  # CPU
        perf = info["perf"]
        cores = info["cores"]
        if perf >= 55:
            lab = "🏆 Excellent pour gaming AAA 2026 (aucun goulot)"
        elif perf >= 40:
            lab = "✅ Très bon pour AAA 2026 en 1080p/1440p"
        elif perf >= 28:
            lab = "🟡 Correct, peut brider les GPU récents en 1080p"
        else:
            lab = "🟠 Ancien: goulot d'étranglement probable avec un GPU moderne"
        return {"perf": perf, "cores": cores, "label": lab,
                "playable_aaa": perf >= 28}


def resell_score(category, model, price, ref, functional):
    """
    Score de REVENDABILITÉ /100 basé sur 7 paramètres.
    Retourne (score, detail_dict).
    """
    info = get_perf(category, model)

    # a) Demande marché
    demand = demand_for(category, model, ref["fair"], info["year"] if info else None)

    # b) marge absolue
    margin = max(0, ref["fair"] - price) if price > 0 else 0

    # c) ratio de marge (%)
    margin_ratio = (margin / price * 100) if price > 0 else 0

    # d) fraîcheur techno (année de la gén)
    year = info["year"] if info else 2019
    age = CURRENT_YEAR - year
    freshness = clamp(100 - age * 9)   # -9 pts/an

    # e) pertinence 2026 (jouable AAA)
    relevance = 0
    if info:
        if category == "GPU":
            relevance = clamp(RES_RANK.get(info["res"], 0) * 25)  # 4K=100.. low=25
        else:
            relevance = clamp(info["perf"])
    # f) bonus VRAM (GPU seulement)
    vram_bonus = 0
    if info and category == "GPU":
        v = info["vram"]
        vram_bonus = 100 if v >= 16 else 75 if v >= 12 else 50 if v >= 8 else 20

    # g) confiance fonctionnelle
    func_score = 100 if functional else 55

    # Pondération (somme = 1.0)
    if category == "GPU":
        score = (demand*0.26 + clamp(margin_ratio*2)*0.18 + freshness*0.14
                 + relevance*0.16 + vram_bonus*0.12 + func_score*0.14)
    elif category == "CPU":
        score = (demand*0.28 + clamp(margin_ratio*2)*0.20 + freshness*0.16
                 + relevance*0.22 + func_score*0.14)
    else:
        # autres composants: pas de perf, on se base sur demande/marge/fonctionnel
        score = (demand*0.4 + clamp(margin_ratio*2)*0.35 + func_score*0.25)

    detail = {
        "demande": round(demand),
        "marge_eur": round(margin),
        "marge_pct": round(margin_ratio),
        "fraicheur": round(freshness),
        "pertinence_2026": round(relevance),
        "vram_bonus": round(vram_bonus) if category == "GPU" else None,
        "fonctionnel": func_score == 100,
    }
    return round(clamp(score)), detail


def deal_score(price, ref):
    """0-100: à quel point le prix est bon vs marché."""
    if price <= 0:
        return 0
    fair = ref["fair"]
    if price >= fair:
        return clamp(round(50 * fair / price))   # au-dessus du juste = <50
    # sous le juste: 50 (à fair) → 100 (à steal ou moins)
    steal = ref["steal"]
    if price <= steal:
        return 100
    # interpolation entre fair (50) et steal (95)
    frac = (fair - price) / max(1, (fair - steal))
    return clamp(round(50 + frac * 45))


def full_report(category, model, price, ref, functional):
    """Rapport complet pour une annonce."""
    ppp = perf_per_price(category, model, price)
    aaa = aaa_verdict(category, model)
    ds = deal_score(price, ref)
    rs, rs_detail = resell_score(category, model, price, ref, functional)

    # Verdict global: pondération deal + revente + perf/prix
    parts = [ds * 0.4, rs * 0.35]
    weight = 0.75
    if ppp is not None:
        parts.append(min(100, ppp) * 0.25)
        weight = 1.0
    global_score = round(clamp(sum(parts) / weight))

    if global_score >= 80:
        verdict = "💎 À SAISIR"
        vcls = "excellent"
    elif global_score >= 65:
        verdict = "✅ BON DEAL"
        vcls = "good"
    elif global_score >= 50:
        verdict = "🟡 CORRECT"
        vcls = "ok"
    else:
        verdict = "🟠 MOYEN / À NÉGOCIER"
        vcls = "meh"

    return {
        "perf_per_price": ppp,
        "aaa": aaa,
        "deal_score": ds,
        "resell_score": rs,
        "resell_detail": rs_detail,
        "global_score": global_score,
        "verdict": verdict,
        "verdict_class": vcls,
        "has_perf": get_perf(category, model) is not None,
    }
