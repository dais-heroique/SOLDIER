"""
relevance.py — Vérifie qu'une annonce correspond VRAIMENT au modèle recherché
═══════════════════════════════════════════════════════════════════════════
Avant ce module, le sniper faisait confiance aveuglément aux résultats de
recherche Leboncoin/Vinted : chercher "RTX 5090" pouvait remonter un laptop,
une alimentation, une carte mère... et l'annonce était quand même étiquetée
"RTX 5090" avec le prix de référence (2100€) → marge fictive énorme → ces
annonces polluaient tout le haut du classement.

Ce module extrait des "tokens" distinctifs à partir du NOM DU MODÈLE
(ex: "RTX 5090" -> ["rtx","5090"], "Ryzen 5 5600X" -> ["ryzen","5600x"])
et exige que TOUS ces tokens soient présents dans le titre+description de
l'annonce avant de l'accepter.

⚠️ Correctif important: pour une capacité/mesure comme "1To", "32GB", "750W",
l'ancienne version gardait UNIQUEMENT le chiffre nu ("1", "32", "750") comme
token requis — beaucoup trop générique (un "1" ou "2" tout seul apparaît dans
une quantité énorme d'annonces sans rapport : "M2", "RTX 2060", "garantie 2
ans"...). Ce module exige maintenant le chiffre ACCOLÉ à son unité (ou une
orthographe équivalente : Go/GB, To/TB, Mo/MB) plutôt qu'un chiffre isolé.
"""

import re

# Mots génériques de catégorie/descripteurs — pas discriminants entre modèles
# ⚠️ "alimentation" et "aio" sont volontairement RETIRÉS de cette liste (pas
# strippés): pour les modèles PSU ("Alimentation 1200W") et COOLING ("AIO
# 420mm"), le nom du modèle ne contient QUE ce mot générique + une mesure —
# si on le stripe, il ne reste que la mesure ("1200w"/"420mm") comme seul
# token requis, qui matche n'importe quel appareil électroménager de la même
# puissance (fer à repasser, grill...) ou produit de la même taille. Pour
# toutes les autres catégories ce mot est absent du nom de modèle donc ça ne
# change rien.
GENERIC_STOP = {
    "carte", "graphique", "mere", "mère", "processeur", "boitier",
    "boîtier", "clavier", "souris", "casque", "ecran", "écran", "siege", "siège",
    "chaise", "psu", "cpu", "gpu", "ram", "memoire", "mémoire", "stockage",
    "refroidissement", "watercooling", "pc", "portable", "tour", "kit",
    "barrette", "de", "du", "la", "le", "et", "avec", "pour", "the", "of", "a",
    "socket", "edition", "series", "complet", "gamer", "gaming",
}
# Équivalences d'unités selon la langue/orthographe (Go=GB, To=TB, Mo=MB...) —
# un chiffre suivi d'UNE de ces variantes est accepté comme la même mesure.
UNIT_EQUIVALENTS = {
    "go": ["go", "gb"], "gb": ["go", "gb"],
    "to": ["to", "tb"], "tb": ["to", "tb"],
    "mo": ["mo", "mb"], "mb": ["mo", "mb"],
    "w": ["w"], "mm": ["mm"], "ghz": ["ghz"], "mhz": ["mhz"],
}
UNIT_STOP = set(UNIT_EQUIVALENTS.keys())
# Suffixes courts mais significatifs quand ils sont un mot séparé (ex: "1050 Ti")
SUFFIX_WHITELIST = {"ti", "xt", "se", "ge", "kf", "xe", "fe"}


def _normalize_flat(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _normalize_words(s):
    return set(w.lower() for w in re.findall(r"[a-zA-Z0-9]+", s or ""))


def extract_tokens(model_name):
    """
    Dérive les tokens obligatoires à partir du nom du modèle (clé du catalogue).
    Chaque token est soit une chaîne simple (substring/mot requis), soit un
    tuple ("cap", chiffre, [variantes_unité]) pour les mesures — au moins une
    des variantes doit être accolée au chiffre dans l'annonce.
    """
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
            if alpha in UNIT_STOP:
                # mesure/capacité: exige le chiffre ACCOLÉ à l'unité (ou une
                # orthographe équivalente), jamais le chiffre tout seul
                tokens.append(("cap", num, tuple(UNIT_EQUIVALENTS[alpha])))
            else:
                # suffixe fusionné non-unité (5600X, 13600K...) -> gardé tel quel
                tokens.append(wl)
            continue
        if wl.isdigit():
            # chiffre isolé sans unité dans le nom du modèle (rare) — on le
            # garde tel quel, c'est déjà le comportement voulu pour ce cas
            tokens.append(wl)
        elif len(wl) >= 3:
            tokens.append(wl)
        elif wl in SUFFIX_WHITELIST:
            tokens.append(wl)
    return list(dict.fromkeys(tokens))  # dédoublonne, garde l'ordre


MAX_DESC_CHARS_FOR_MATCH = 220  # anti-bourrage: au-delà, on ignore le reste de la
# description pour la vérification de pertinence. Certains vendeurs collent une
# longue liste de mots-clés/modèles compatibles dans la description pour apparaître
# dans plus de recherches, même quand ce n'est pas le bon produit — en limitant la
# portion de texte analysée, on réduit le risque de faux positifs de ce genre.


def model_matches(subject, description, tokens, exclude=None):
    """
    True si TOUS les tokens du modèle sont retrouvés dans le texte de l'annonce
    ET qu'aucun token d'exclusion n'est présent. Les tokens d'exclusion servent
    à départager des modèles proches (ex: "RTX 4070" de base ne doit PAS matcher
    une annonce "RTX 4070 Ti SUPER" — "ti"/"super" sont alors en exclusion pour
    "RTX 4070" de base). Voir build_token_cache pour leur calcul automatique.
    """
    if not tokens:
        return True
    capped_desc = (description or "")[:MAX_DESC_CHARS_FOR_MATCH]
    text = (subject or "") + " " + capped_desc
    flat = _normalize_flat(text)
    words = _normalize_words(text)
    for t in tokens:
        if isinstance(t, tuple) and t[0] == "cap":
            _, num, variants = t
            # au moins une orthographe du chiffre+unité doit être accolée dans le texte
            if not any((num + v) in flat for v in variants):
                return False
        elif len(t) <= 2:
            if t not in words:      # tokens courts (ex: "ti") -> mot exact requis
                return False
        else:
            if t not in flat:       # tokens longs -> sous-chaîne suffit (tolère espaces/langue)
                return False
    if exclude:
        for t in exclude:
            if len(t) <= 2:
                if t in words:
                    return False
            else:
                if t in flat:
                    return False
    return True


def build_token_cache(categories):
    """
    Précalcule les tokens de tous les modèles au démarrage (évite de le refaire
    à chaque annonce scannée), ET calcule pour chaque modèle les tokens
    d'EXCLUSION nécessaires pour ne pas être confondu avec un modèle "cousin"
    plus spécifique de la même catégorie (ex: "RTX 4070" de base vs "RTX 4070
    Ti", "RTX 4070 SUPER", "RTX 4070 Ti SUPER" — sans ça, une annonce Ti SUPER
    satisferait AUSSI les tokens du modèle de base, et son prix serait comparé
    au mauvais seuil de référence).

    Retourne {(cat, model): {"require": [...], "exclude": [...]}}.
    """
    cache = {}
    for cat_key, cat in categories.items():
        model_tokens = {m: extract_tokens(m) for m in cat["db"]}
        for model_name, tokens in model_tokens.items():
            base = set(tokens)
            exclude = set()
            for other_name, other_tokens in model_tokens.items():
                if other_name == model_name:
                    continue
                other = set(other_tokens)
                # si `base` est un sous-ensemble STRICT de `other`, alors `other`
                # est un modèle plus spécifique -> les tokens en trop de `other`
                # deviennent des exclusions pour ce modèle-ci (base)
                if base and base < other:
                    exclude |= (other - base)
            cache[(cat_key, model_name)] = {
                "require": tokens,
                "exclude": [t for t in exclude if isinstance(t, str)],  # les tuples "cap" ne s'excluent pas (ambigu), on ne garde que les mots
            }
    return cache
