"""
settings_store.py — Paramètres persistants (pays, langue, plateformes actives)
═══════════════════════════════════════════════════════════════════════════
Permet de changer le pays, la langue de l'interface, et quelles plateformes
sont actives DIRECTEMENT DEPUIS LE DASHBOARD, sans toucher au terminal ni
relancer l'app. Les changements sont persistés dans sniper_settings.json et
appliqués par le thread de scan au cycle suivant.

Priorité de résolution du pays: variable d'environnement SNIPER_COUNTRY (si
définie, prioritaire — utile pour un déploiement automatisé) > fichier de
paramètres > défaut FR.
"""

import json
import os

SETTINGS_FILE = "sniper_settings.json"

DEFAULT_SETTINGS = {
    "country": "FR",
    "lang": "fr",
    "sources": {"lbc": True, "vinted": True, "ebay": True, "facebook": True},
    "location": "",   # ville/code postal pour Facebook Marketplace (vide = défaut du pays)
    "radius_km": 16,  # rayon de recherche Facebook Marketplace autour de "location"
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            settings.update(saved)
            if "sources" in saved:
                settings["sources"] = {**DEFAULT_SETTINGS["sources"], **saved["sources"]}
        except Exception:
            pass
    # la variable d'environnement reste prioritaire si définie explicitement
    env_country = os.environ.get("SNIPER_COUNTRY")
    if env_country:
        settings["country"] = env_country.upper()
    env_location = os.environ.get("SNIPER_LOCATION")
    if env_location:
        settings["location"] = env_location
    if os.environ.get("SNIPER_NO_LBC") == "1":
        settings["sources"]["lbc"] = False
    if os.environ.get("SNIPER_NO_VINTED") == "1":
        settings["sources"]["vinted"] = False
    if os.environ.get("SNIPER_NO_EBAY") == "1":
        settings["sources"]["ebay"] = False
    if os.environ.get("SNIPER_NO_FACEBOOK") == "1":
        settings["sources"]["facebook"] = False
    return settings


def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
