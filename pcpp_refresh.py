"""
pcpp_refresh.py — Rafraîchit les prix de référence avec de VRAIS prix neufs
═══════════════════════════════════════════════════════════════════════════
Récupère les prix neufs actuels sur PCPartPicker (cartes graphiques +
processeurs), les fait correspondre à notre catalogue via le même système de
tokens que le filtre de pertinence, applique une courbe de décote par âge
pour estimer un prix occasion réaliste, et écrit live_prices.json.

⚠️ Comme Leboncoin/Vinted, PCPartPicker bloque les IP de datacenter. Ce
script doit tourner sur TON Mac (IP résidentielle), pas dans un cloud.

Usage (depuis ton venv, avec l'app arrêtée ou en marche, peu importe):
    pip install "git+https://github.com/nynhex/PCPartPicker-API.git"
    python3 pcpp_refresh.py

Résultat: live_prices.json est mis à jour. app.py le relit automatiquement
au prochain cycle de scan (pas besoin de relancer l'app).

Relance ce script de temps en temps (une fois par semaine par ex.) pour
garder les prix neufs à jour — pas besoin de le laisser tourner en continu.
"""

import json
import re
import sys
import time
from datetime import datetime

from market_db import CATEGORIES
from perf_db import GPU_PERF, CPU_PERF
from relevance import extract_tokens, model_matches
from price_resolver import new_price_to_occasion, LIVE_PRICES_FILE

try:
    from PCPartPicker_API import pcpartpicker as pcpp
except ImportError:
    print("❌ PCPartPicker_API n'est pas installé.")
    print('   Lance: pip install "git+https://github.com/nynhex/PCPartPicker-API.git"')
    sys.exit(1)

CURRENT_YEAR = 2026

# Catégories couvertes (celles où PCPartPicker a des pages produit dédiées
# ET où on a des données de performance/année pour calculer la décote)
PARTTYPE_MAP = {
    "GPU": "video-card",
    "CPU": "cpu",
}


def parse_price(raw):
    """Extrait un nombre depuis une chaîne de prix PCPartPicker ('329,99 €', '$329.99'...)."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", raw)
    cleaned = cleaned.replace(",", ".")
    # garde seulement le dernier point comme séparateur décimal si plusieurs
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


def fetch_partype(part_type):
    """Récupère tout le catalogue PCPartPicker pour un type de pièce (toutes pages)."""
    print(f"  📥 Téléchargement du catalogue PCPartPicker '{part_type}' (toutes pages)...")
    items = pcpp.productLists.getProductList(part_type, pageNum=0)
    print(f"     -> {len(items)} produits récupérés")
    return items


def refresh_category(cat_key, part_type, perf_db):
    catalog_models = CATEGORIES[cat_key]["db"]
    try:
        items = fetch_partype(part_type)
    except Exception as e:
        print(f"  ❌ Échec récupération '{part_type}': {e}")
        print("     (normal si tu es sur une IP bloquée — relance depuis ton Mac perso)")
        return {}

    parsed = []
    for it in items:
        name = it.get("name", "")
        price = parse_price(it.get("price", ""))
        if name and price:
            parsed.append((name, price))

    # tokens de chaque modèle du catalogue, précalculés une fois
    model_tokens = {m: extract_tokens(m) for m in catalog_models}

    # Assigne chaque produit PCPartPicker au modèle le PLUS SPÉCIFIQUE qui matche
    # (évite qu'une fiche "RTX 4070 Ti SUPER" se fasse aussi compter pour "RTX 4070 Ti"
    # ou "RTX 4070 SUPER" — seul le modèle avec le plus de tokens requis l'emporte)
    assigned = {m: [] for m in catalog_models}
    for name, price in parsed:
        candidates = [m for m, toks in model_tokens.items() if model_matches(name, "", toks)]
        if not candidates:
            continue
        best_len = max(len(model_tokens[m]) for m in candidates)
        tied = [m for m in candidates if len(model_tokens[m]) == best_len]
        if len(tied) != 1:
            continue  # ambigu entre plusieurs modèles à égalité de spécificité -> on ignore
        assigned[tied[0]].append(price)

    results = {}
    for model_name, matches in assigned.items():
        if not matches:
            continue
        new_price = min(matches)  # prix neuf le plus bas actuellement trouvé (street price réel)

        info = perf_db.get(model_name)
        year = info["year"] if info else None
        age = (CURRENT_YEAR - year) if year else 3  # âge par défaut si inconnu

        fair = new_price_to_occasion(cat_key, new_price, age)
        results[f"{cat_key}::{model_name}"] = {
            "new_price": round(new_price),
            "fair": fair,
            "age_years": age,
            "matches_found": len(matches),
            "fetched_at": datetime.now().strftime("%d/%m/%Y"),
        }
        print(f"     ✓ {model_name:22} neuf {round(new_price):>5}€ (min sur {len(matches)} offres) "
              f"-> occasion estimée {fair}€")
    return results


def main():
    pcpp.setRegion("fr")  # prix directement en euros
    print("🔧 Rafraîchissement des prix de référence via PCPartPicker\n")

    all_results = {}
    for cat_key, part_type in PARTTYPE_MAP.items():
        print(f"📦 {CATEGORIES[cat_key]['label']}")
        perf_db = GPU_PERF if cat_key == "GPU" else CPU_PERF
        results = refresh_category(cat_key, part_type, perf_db)
        all_results.update(results)
        print()
        time.sleep(1)

    if not all_results:
        print("⚠️  Aucun prix récupéré. Si tu vois des erreurs de connexion/403 ci-dessus,")
        print("    c'est que PCPartPicker bloque cette IP — lance ce script depuis ton Mac")
        print("    personnel (pas un serveur/cloud), avec une connexion internet normale.")
        return

    with open(LIVE_PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"✅ {len(all_results)} modèles mis à jour dans {LIVE_PRICES_FILE}")
    print("   L'app relira ces prix automatiquement au prochain cycle de scan.")


if __name__ == "__main__":
    main()
