"""
price_resolver.py — Résolution du "vrai" prix de référence par modèle
═══════════════════════════════════════════════════════════════════════════
Le tableau statique de market_db.py était une première estimation (utile
pour démarrer) mais figée et pas vérifiée modèle par modèle. Ce module la
remplace par une résolution EN COUCHES, la plus fiable disponible en premier :

  1. 🥇 DONNÉES RÉELLES OBSERVÉES (price_history.py) — une fois qu'on a assez
     d'annonces vues sur Leboncoin/Vinted pour un modèle (seuil MIN_OBS), le
     prix médian RÉELLEMENT constaté sur le marché occasion devient la
     référence. C'est la source la plus fiable qui soit : c'est le marché lui-même.

  2. 🥈 PCPARTPICKER (live_prices.json, généré par pcpp_refresh.py) — prix NEUF
     réel récupéré sur PCPartPicker, converti en estimation occasion via une
     courbe de décote par âge. Bien plus fiable que deviner à la main, mais
     indirect (neuf -> occasion estimé, pas observé).

  3. 🥉 TABLE STATIQUE (market_db.py) — estimation de base, utilisée seulement
     si rien de mieux n'est disponible.

Le résultat est un dict {"fair","good","steal"} identique en forme à ceux de
market_db.py, mais avec en plus un champ "source" indiquant sa provenance —
affiché dans l'interface pour que tu saches à quel point te fier au chiffre.
"""

import json
import os
from datetime import datetime

LIVE_PRICES_FILE = "live_prices.json"
MIN_OBS_FOR_MARKET_PRICE = 15   # nb mini d'annonces vues avant de faire confiance au marché réel

# Courbes de décote occasion par âge (GPU se déprécie plus vite que CPU,
# les sockets CPU restant compatibles plus longtemps -> demande soutenue)
GPU_DEPRECIATION = [
    (1, 0.78), (2, 0.62), (3, 0.50), (4, 0.40), (5, 0.32), (8, 0.22), (99, 0.15),
]
CPU_DEPRECIATION = [
    (1, 0.82), (2, 0.68), (3, 0.58), (4, 0.48), (5, 0.40), (8, 0.28), (99, 0.18),
]
DEFAULT_DEPRECIATION = [
    (1, 0.80), (2, 0.65), (3, 0.52), (4, 0.42), (5, 0.34), (8, 0.24), (99, 0.16),
]


def _depreciation_factor(category, age_years):
    curve = {"GPU": GPU_DEPRECIATION, "CPU": CPU_DEPRECIATION}.get(category, DEFAULT_DEPRECIATION)
    for max_age, factor in curve:
        if age_years <= max_age:
            return factor
    return curve[-1][1]


def new_price_to_occasion(category, new_price, age_years):
    """Convertit un prix neuf PCPartPicker en estimation de prix occasion juste."""
    factor = _depreciation_factor(category, max(0, age_years))
    return round(new_price * factor)


def _load_live_prices():
    if os.path.exists(LIVE_PRICES_FILE):
        try:
            with open(LIVE_PRICES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


class PriceResolver:
    def __init__(self, price_history=None):
        """price_history: instance de PriceHistory (price_history.py), optionnelle."""
        self.history = price_history
        self.live = _load_live_prices()

    def reload_live(self):
        self.live = _load_live_prices()

    def resolve(self, cat, model, static_ref):
        """
        Retourne un dict {"fair","good","steal","source","note"} pour ce modèle.
        static_ref = ref["queries"] etc. du market_db (utilisé comme fallback ultime).
        """
        # 1) marché réel observé
        if self.history is not None:
            stats = self.history.stats(cat, model)
            if stats and stats.get("n", 0) >= MIN_OBS_FOR_MARKET_PRICE:
                fair = stats["med"]
                return {
                    "fair": fair, "good": round(fair * 0.83), "steal": round(fair * 0.68),
                    "source": "marché réel", "queries": static_ref.get("queries", []),
                    "watts": static_ref.get("watts"),
                    "note": f"basé sur {stats['n']} annonces observées (min {stats['min']}€ / max {stats['max']}€)",
                }

        # 2) PCPartPicker (neuf -> occasion estimé)
        key = f"{cat}::{model}"
        live = self.live.get(key)
        if live and live.get("fair"):
            return {
                "fair": live["fair"], "good": round(live["fair"] * 0.83),
                "steal": round(live["fair"] * 0.68), "source": "PCPartPicker",
                "queries": static_ref.get("queries", []), "watts": static_ref.get("watts"),
                "note": f"neuf {live.get('new_price','?')}€ (PCPartPicker, {live.get('fetched_at','?')}) "
                        f"× décote âge",
            }

        # 3) table statique (fallback)
        return {
            "fair": static_ref["fair"], "good": static_ref["good"], "steal": static_ref["steal"],
            "source": "estimation", "queries": static_ref.get("queries", []),
            "watts": static_ref.get("watts"),
            "note": "estimation de départ, pas encore confirmée par le marché ou PCPartPicker",
        }
