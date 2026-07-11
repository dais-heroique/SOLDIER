"""
price_history.py — Historique des prix par modèle
═══════════════════════════════════════════════════════════════════════
À chaque scan, on stocke les prix observés (toutes annonces valides, pas
que les deals) pour chaque modèle. Deux niveaux de stockage :

  1. AGRÉGATS QUOTIDIENS (price_history.json) — léger, rapide à charger,
     c'est ce qui alimente le graphique de l'évaluateur. Le détail brut
     ("samples") d'une journée n'est gardé que pour la journée EN COURS
     (nécessaire pour fusionner les nouvelles observations du jour) ; les
     jours passés ne gardent que l'agrégat (min/médian/moyenne/max/n).

  2. LOG PERMANENT (price_observations.jsonl) — CHAQUE prix observé,
     individuellement, avec horodatage, écrit en continu (append-only) et
     JAMAIS supprimé. Avec beaucoup d'espace disque disponible, ça permet
     de garder un historique complet et brut pour des analyses futures
     (saisonnalité, volatilité par heure/jour, recalcul de tendances...)
     sans jamais rien perdre — ce que la version précédente (contrainte
     en espace) devait sacrifier au fil du temps.

Rétention des agrégats quotidiens (price_history.json): RETENTION_DAYS,
réglé très large maintenant que l'espace n'est plus une contrainte — les
agrégats sont de toute façon minuscules (quelques nombres par jour).

Stockage: price_history.json
Structure:
{
  "GPU::RTX 3060": {
     "daily": {
        "2026-06-30": {"min":180,"med":200,"avg":205,"max":250,"n":12},         # jour passé: agrégat seul
        "2026-07-01": {"min":175,"med":198,"avg":201,"max":240,"n":8,"samples":[...]}  # jour courant: + détail
     }
  }, ...
}
"""

import json
import os
from datetime import datetime, timedelta
from statistics import median, mean

HISTORY_FILE = "price_history.json"
RAW_LOG_FILE = "price_observations.jsonl"   # archive permanente, jamais purgée
RETENTION_DAYS = 3650    # ~10 ans d'agrégats quotidiens glissants (coût quasi nul en espace)
MAX_SAMPLES_TODAY = 2000  # garde-fou sur le jour en cours (large, l'espace n'est plus un souci)


def _log_raw_observations(cat, model, prices, tstr):
    """Ajoute chaque prix observé à l'archive brute permanente (jamais supprimée)."""
    try:
        with open(RAW_LOG_FILE, "a", encoding="utf-8") as f:
            for p in prices:
                f.write(json.dumps({"t": tstr, "cat": cat, "model": model, "price": p},
                                   ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save(data):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


class PriceHistory:
    def __init__(self):
        self.data = _load()

    def key(self, cat, model):
        return f"{cat}::{model}"

    def record(self, cat, model, prices):
        """Enregistre les prix observés pour un modèle à l'instant T : dans
        l'agrégat quotidien (léger, pour le graphique) ET dans l'archive
        brute permanente (jamais supprimée)."""
        prices = [p for p in prices if p and p > 0]
        if not prices:
            return
        k = self.key(cat, model)
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        tstr = now.strftime("%Y-%m-%dT%H:%M:%S")

        _log_raw_observations(cat, model, prices, tstr)   # archive permanente

        node = self.data.setdefault(k, {"daily": {}})
        daily = node["daily"]

        d = daily.get(today)
        allp = list(prices)
        if d and "samples" in d:
            allp = d["samples"] + allp
        allp = allp[-MAX_SAMPLES_TODAY:]

        daily[today] = {
            "min": min(allp), "med": round(median(allp)),
            "avg": round(mean(allp)), "max": max(allp),
            "n": len(allp), "samples": allp,
        }

        # allège tous les jours qui ne sont PAS aujourd'hui (plus besoin du détail brut
        # dans le JSON léger — le détail brut complet reste pour toujours dans l'archive)
        for day, entry in daily.items():
            if day != today and "samples" in entry:
                del entry["samples"]

        # rétention: purge les jours trop vieux
        cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
        for day in [d for d in daily if d < cutoff]:
            del daily[day]

    def stats(self, cat, model):
        """Stats globales pour un modèle, recalculées depuis les agrégats journaliers
        (fonctionne même sans les samples bruts des jours passés)."""
        k = self.key(cat, model)
        node = self.data.get(k)
        if not node or not node.get("daily"):
            return None
        days = node["daily"]
        mins = [d["min"] for d in days.values()]
        maxs = [d["max"] for d in days.values()]
        meds = [d["med"] for d in days.values()]
        n = sum(d["n"] for d in days.values())
        return {
            "min": min(mins), "max": max(maxs),
            "med": round(median(meds)), "avg": round(mean(meds)), "n": n,
        }

    def series(self, cat, model):
        """Série journalière triée (pour le graphique)."""
        k = self.key(cat, model)
        node = self.data.get(k)
        if not node:
            return []
        days = sorted(node["daily"].keys())
        return [{"day": day, "min": node["daily"][day]["min"],
                 "med": node["daily"][day]["med"], "max": node["daily"][day]["max"],
                 "n": node["daily"][day]["n"]} for day in days]

    def export_compact(self):
        """Version pour injecter dans le dashboard (déjà sans samples pour les jours passés)."""
        out = {}
        for k, node in self.data.items():
            out[k] = {"daily": {day: {kk: vv for kk, vv in d.items() if kk != "samples"}
                                for day, d in node["daily"].items()}}
        return out

    def prune_empty(self):
        """Supprime les modèles qui n'ont plus aucun jour en historique."""
        empty = [k for k, node in self.data.items() if not node.get("daily")]
        for k in empty:
            del self.data[k]

    def save(self):
        self.prune_empty()
        _save(self.data)
