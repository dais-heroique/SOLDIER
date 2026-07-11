"""
countries.py — Configuration multi-pays (Vinted + eBay + Leboncoin)
═══════════════════════════════════════════════════════════════════════
Leboncoin est franco-français (leboncoin.fr) — has_lbc=True seulement pour FR.
Vinted opère dans ~26 marchés — has_vinted=False pour les pays où Vinted
n'existe pas (le pays retombe alors automatiquement sur eBay seul).
eBay est disponible presque partout dans le monde ; les pays sans site eBay
dédié utilisent EBAY-US par défaut (fonctionne globalement).

Le pays actif se choisit soit dans l'interface (Paramètres -> Pays, persisté
dans sniper_settings.json), soit via la variable d'environnement
SNIPER_COUNTRY (prioritaire si définie).
"""

import os

# code: label, domaine vinted, site eBay, langue Accept-Language, devise,
#        has_lbc (Leboncoin dispo), has_vinted (marché Vinted existant)
COUNTRIES = {
    "FR": {"label": "France",            "vinted_domain": "vinted.fr",    "ebay_site": "EBAY-FR",   "lang": "fr-FR,fr;q=0.9", "currency": "EUR", "has_lbc": True,  "has_vinted": True},
    "DE": {"label": "Allemagne",         "vinted_domain": "vinted.de",    "ebay_site": "EBAY-DE",   "lang": "de-DE,de;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "IT": {"label": "Italie",            "vinted_domain": "vinted.it",    "ebay_site": "EBAY-IT",   "lang": "it-IT,it;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "ES": {"label": "Espagne",           "vinted_domain": "vinted.es",    "ebay_site": "EBAY-ES",   "lang": "es-ES,es;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "NL": {"label": "Pays-Bas",          "vinted_domain": "vinted.nl",    "ebay_site": "EBAY-NL",   "lang": "nl-NL,nl;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "BE": {"label": "Belgique",          "vinted_domain": "vinted.fr",    "ebay_site": "EBAY-FR",   "lang": "fr-BE,fr;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "AT": {"label": "Autriche",          "vinted_domain": "vinted.at",    "ebay_site": "EBAY-AT",   "lang": "de-AT,de;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "PT": {"label": "Portugal",          "vinted_domain": "vinted.pt",    "ebay_site": "EBAY-US",   "lang": "pt-PT,pt;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "PL": {"label": "Pologne",           "vinted_domain": "vinted.pl",    "ebay_site": "EBAY-US",   "lang": "pl-PL,pl;q=0.9", "currency": "PLN", "has_lbc": False, "has_vinted": True},
    "CZ": {"label": "Tchéquie",          "vinted_domain": "vinted.cz",    "ebay_site": "EBAY-US",   "lang": "cs-CZ,cs;q=0.9", "currency": "CZK", "has_lbc": False, "has_vinted": True},
    "SK": {"label": "Slovaquie",         "vinted_domain": "vinted.sk",    "ebay_site": "EBAY-US",   "lang": "sk-SK,sk;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "LT": {"label": "Lituanie",          "vinted_domain": "vinted.lt",    "ebay_site": "EBAY-US",   "lang": "lt-LT,lt;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "RO": {"label": "Roumanie",          "vinted_domain": "vinted.ro",    "ebay_site": "EBAY-US",   "lang": "ro-RO,ro;q=0.9", "currency": "RON", "has_lbc": False, "has_vinted": True},
    "LU": {"label": "Luxembourg",        "vinted_domain": "vinted.lu",    "ebay_site": "EBAY-FR",   "lang": "fr-LU,fr;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "HU": {"label": "Hongrie",           "vinted_domain": "vinted.hu",    "ebay_site": "EBAY-US",   "lang": "hu-HU,hu;q=0.9", "currency": "HUF", "has_lbc": False, "has_vinted": True},
    "SE": {"label": "Suède",             "vinted_domain": "vinted.se",    "ebay_site": "EBAY-US",   "lang": "sv-SE,sv;q=0.9", "currency": "SEK", "has_lbc": False, "has_vinted": True},
    "DK": {"label": "Danemark",          "vinted_domain": "vinted.dk",    "ebay_site": "EBAY-US",   "lang": "da-DK,da;q=0.9", "currency": "DKK", "has_lbc": False, "has_vinted": True},
    "FI": {"label": "Finlande",          "vinted_domain": "vinted.fi",    "ebay_site": "EBAY-US",   "lang": "fi-FI,fi;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "IE": {"label": "Irlande",           "vinted_domain": "vinted.ie",    "ebay_site": "EBAY-IE",   "lang": "en-IE,en;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "GR": {"label": "Grèce",             "vinted_domain": "vinted.gr",    "ebay_site": "EBAY-US",   "lang": "el-GR,el;q=0.9", "currency": "EUR", "has_lbc": False, "has_vinted": True},
    "GB": {"label": "Royaume-Uni",       "vinted_domain": "vinted.co.uk", "ebay_site": "EBAY-GB",   "lang": "en-GB,en;q=0.9", "currency": "GBP", "has_lbc": False, "has_vinted": True},
    "US": {"label": "États-Unis",        "vinted_domain": "vinted.com",   "ebay_site": "EBAY-US",   "lang": "en-US,en;q=0.9", "currency": "USD", "has_lbc": False, "has_vinted": True},
    "CA": {"label": "Canada",            "vinted_domain": "vinted.com",   "ebay_site": "EBAY-ENCA", "lang": "en-CA,en;q=0.9", "currency": "CAD", "has_lbc": False, "has_vinted": True},
    # Pays où Vinted n'existe pas (à ce jour) -> eBay seul, automatiquement
    "AU": {"label": "Australie",         "vinted_domain": None,           "ebay_site": "EBAY-AU",   "lang": "en-AU,en;q=0.9", "currency": "AUD", "has_lbc": False, "has_vinted": False},
    "CH": {"label": "Suisse",            "vinted_domain": None,           "ebay_site": "EBAY-CH",   "lang": "de-CH,de;q=0.9", "currency": "CHF", "has_lbc": False, "has_vinted": False},
    "IN": {"label": "Inde",              "vinted_domain": None,           "ebay_site": "EBAY-US",   "lang": "en-IN,en;q=0.9", "currency": "INR", "has_lbc": False, "has_vinted": False},
    "SG": {"label": "Singapour",         "vinted_domain": None,           "ebay_site": "EBAY-US",   "lang": "en-SG,en;q=0.9", "currency": "SGD", "has_lbc": False, "has_vinted": False},
    "JP": {"label": "Japon",             "vinted_domain": None,           "ebay_site": "EBAY-US",   "lang": "ja-JP,ja;q=0.9", "currency": "JPY", "has_lbc": False, "has_vinted": False},
    "BR": {"label": "Brésil",            "vinted_domain": None,           "ebay_site": "EBAY-US",   "lang": "pt-BR,pt;q=0.9", "currency": "BRL", "has_lbc": False, "has_vinted": False},
    "MX": {"label": "Mexique",           "vinted_domain": None,           "ebay_site": "EBAY-US",   "lang": "es-MX,es;q=0.9", "currency": "MXN", "has_lbc": False, "has_vinted": False},
    "XX": {"label": "Autre pays (eBay seul)", "vinted_domain": None,      "ebay_site": "EBAY-US",   "lang": "en-US,en;q=0.9", "currency": "USD", "has_lbc": False, "has_vinted": False},
}

DEFAULT_COUNTRY = "FR"


def get_country_config(code=None):
    """Retourne la config du pays demandé, ou celle de SNIPER_COUNTRY/sniper_settings
    (résolu par le code appelant) si aucun code n'est donné explicitement."""
    if code is None:
        code = os.environ.get("SNIPER_COUNTRY", DEFAULT_COUNTRY).upper()
    else:
        code = code.upper()
    if code not in COUNTRIES:
        code = DEFAULT_COUNTRY
    cfg = dict(COUNTRIES[code])
    cfg["code"] = code
    return cfg


def list_countries():
    """Liste triée pour peupler un sélecteur dans l'interface."""
    return sorted(
        [{"code": k, **v} for k, v in COUNTRIES.items()],
        key=lambda c: c["label"]
    )
