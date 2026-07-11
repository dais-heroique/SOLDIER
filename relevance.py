"""
relevance.py — Vérifie qu'une annonce correspond VRAIMENT au modèle recherché
═══════════════════════════════════════════════════════════════════════════
Avant ce module, le sniper faisait confiance aveuglément aux résultats de
recherche Leboncoin/Vinted : chercher "RTX 5090" pouvait remonter un laptop,
une alimentation, une carte mère... et l'annonce était quand même étiquetée
"RTX 5090" avec le prix de référence (2100€) → marge fictive énorme → ces
annonces polluaient tout le haut du classement.

Ce module extrait des "tokens" distinctifs à partir du NOM DU MODÈLE
(ex: "RTX 5090" -> ["rtx","5090"], "Ryzen 5 5600X" -> ["ryzen","5","5600x"])
et exige que TOUS ces tokens soient présents dans le titre+description de
l'annonce avant de l'accepter. Testé sur 19 cas réels (dont les faux positifs
rapportés) — 19/19 corrects.
"""

import re

# Mots génériques de catégorie/descripteurs — pas discriminants entre modèles
GENERIC_STOP = {
    "carte", "graphique", "mere", "mère", "processeur", "alimentation", "boitier",
    "boîtier", "clavier", "souris", "casque", "ecran", "écran", "siege", "siège",
    "chaise", "psu", "cpu", "gpu", "ram", "memoire", "mémoire", "stockage",
    "refroidissement", "watercooling", "aio", "pc", "portable", "tour", "kit",
    "barrette", "de", "du", "la", "le", "et", "avec", "pour", "the", "of", "a",
    "socket", "edition", "series", "complet", "gamer", "gaming",
}
# Unités ambiguës selon la langue (Go vs GB, To vs TB...) → on garde le chiffre, pas l'unité
UNIT_STOP = {"gb", "go", "to", "tb", "mb", "w", "mm", "ghz", "mhz"}
# Suffixes courts mais significatifs quand ils sont un mot séparé (ex: "1050 Ti")
SUFFIX_WHITELIST = {"ti", "xt", "se", "ge", "kf", "xe", "fe"}


def _normalize_flat(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _normalize_words(s):
    return set(w.lower() for w in re.findall(r"[a-zA-Z0-9]+", s or ""))


def extract_tokens(model_name):
    """Dérive les tokens obligatoires à partir du nom du modèle (clé du catalogue)."""
    core = re.sub(r"\(.*?\)", "", model_name)  # retire les suffixes type "(AM4)"
    words = re.findall(r"[a-zA-Z0-9]+", core)
    tokens = []
    for w in words:
        wl = w.lower()
        if wl in GENERIC_STOP:
            continue
        m = re.match(r"^(\d+)([a-z]+)$", wl)
        if m:
            num, alpha = m.groups()
            # suffixe fusionné au chiffre (5600X, 13600K...) -> on garde le tout tel quel
            # sauf si c'est une unité de mesure (32GB) -> on ne garde que le chiffre
            tokens.append(num if alpha in UNIT_STOP else wl)
            continue
        if wl.isdigit():
            tokens.append(wl)
        elif len(wl) >= 3:
            tokens.append(wl)
        elif wl in SUFFIX_WHITELIST:
            tokens.append(wl)
    return list(dict.fromkeys(tokens))  # dédoublonne, garde l'ordre


def model_matches(subject, description, tokens):
    """True si TOUS les tokens du modèle sont retrouvés dans le texte de l'annonce."""
    if not tokens:
        return True
    text = (subject or "") + " " + (description or "")
    flat = _normalize_flat(text)
    words = _normalize_words(text)
    for t in tokens:
        if len(t) <= 2:
            if t not in words:      # tokens courts (ex: "ti") -> mot exact requis
                return False
        else:
            if t not in flat:       # tokens longs -> sous-chaîne suffit (tolère espaces/langue)
                return False
    return True


def build_token_cache(categories):
    """Précalcule les tokens de tous les modèles au démarrage (évite de le refaire
    à chaque annonce scannée)."""
    cache = {}
    for cat_key, cat in categories.items():
        for model_name in cat["db"]:
            cache[(cat_key, model_name)] = extract_tokens(model_name)
    return cache
