"""
arnaque.py — Risque d'arnaque d'une annonce, commun à la RAM et aux composants
═══════════════════════════════════════════════════════════════════════════
Un seul score 0-100, deux sources de signaux :

  1. le texte : listing_filter.py (pression, contact hors plateforme, acompte,
     photo volée, prétextes…) — déjà éprouvé côté SOLDIER, que la RAM n'utilisait
     pas du tout ;
  2. le prix rapporté à la valeur du marché : « RTX 4090 à 400 € » n'a pas
     besoin d'un mot suspect pour être une arnaque.

À partir de `arnaque.seuil_rejet` (60), l'annonce est écartée et jamais
notifiée. Entre `seuil_alerte` (30) et le rejet, elle est notifiée avec un
drapeau ⚠️ qui dit POURQUOI — la décision reste à l'acheteur. Le vendeur sans
avis est déjà filtré en amont (vendeur_avis.py) ; ici on ne regarde que ce
qu'un vendeur avec 1 ou 2 avis peut encore cacher.

Tout est réglable dans ram_config.yaml (section `arnaque`).
"""

import re
import unicodedata

import ram_config

try:
    import listing_filter
except Exception:                                       # pragma: no cover
    listing_filter = None


def _sans_accents(texte):
    texte = unicodedata.normalize("NFKD", texte or "")
    return "".join(c for c in texte if not unicodedata.combining(c)).lower()


# Signaux que listing_filter ne couvre pas. Poids = force du signal, sur le
# texte sans accents. Un seul signal « fort » (≥ 60) suffit à écarter.
SIGNAUX_SUPPLEMENTAIRES = {
    "paiement hors plateforme": {
        r"\bvirement (uniquement|obligatoire|bancaire)\b": 30,
        r"\blydia\b": 25,
        r"\bpaylib\b": 20,
        r"\b(bitcoin|crypto\w*|usdt)\b": 45,
        r"\bcheque de banque\b": 40,
        r"\blien (de )?(paiement|securise|reservation)\b": 45,
    },
    "contact hors plateforme": {
        r"\b(instagram|snap(chat)?|facebook|messenger)\b": 25,
        r"[\w.+-]+@[\w-]+\.[a-z]{2,}": 50,
        r"(?<!\d)(?:\+33|0033|0)\s?[67](?:[ .-]?\d{2}){4}(?!\d)": 25,
    },
    "histoire d'arnaque classique": {
        r"\b(a l'etranger|expatrie|en deplacement a l'etranger|militaire)\b": 40,
        r"\b(mon|un) (transporteur|agent|coursier|livreur)\b": 35,
        r"\b(ramassera|viendra chercher)\b.{0,30}\b(transporteur|agent|coursier)\b": 40,
        r"\bcode (de )?(verification|sms|confirmation)\b": 65,
        r"\b(verifier|valider) (votre|ton|vos) (compte|identite|paiement)\b": 50,
    },
}

DEFAUTS = {
    "actif": True,
    "seuil_rejet": 60,
    "seuil_alerte": 30,
    # Prix / valeur de marché en dessous duquel le prix seul devient suspect.
    "prix_tres_bas_ratio": 0.40,
    "prix_suspect_ratio": 0.55,
    "points_prix_tres_bas": 35,
    "points_prix_suspect": 15,
    # Vendeur à peine établi (1-2 avis) ET prix très bas : profil de faux avis.
    "vendeur_peu_avis": 2,
    "points_vendeur_neuf_prix_bas": 15,
}


def reglages(cfg=None):
    cfg = cfg or ram_config.get()
    sec = dict(DEFAUTS)
    sec.update(cfg.section("arnaque") or {})
    return sec


def evaluer(titre, description="", prix=0.0, valeur_ref=None, nb_photos=None,
            vendeur_avis=None, cfg=None):
    """Retourne {score, niveau, raisons, rejeter}.

    valeur_ref : prix de revente / valeur marché de CE qu'on achète (même
    périmètre que `prix`). None = inconnu : seuls les signaux texte comptent.
    niveau : "faible" | "moyen" | "eleve".
    """
    r = reglages(cfg)
    if not r.get("actif", True):
        return {"score": 0, "niveau": "faible", "raisons": [], "rejeter": False}

    texte = ((titre or "") + " " + (description or "")).lower()
    prix = float(prix or 0)
    valeur = float(valeur_ref) if valeur_ref else 0.0

    score, raisons = 0, []
    if listing_filter is not None:
        base, _neuf, base_raisons = listing_filter._assess_scam_risk(texte, prix, valeur)
        score += base
        raisons += list(base_raisons)

    norm = _sans_accents(texte)
    for libelle, signaux in SIGNAUX_SUPPLEMENTAIRES.items():
        points = sum(p for motif, p in signaux.items() if re.search(motif, norm))
        if points:
            score += points
            raisons.append(libelle)

    ratio = (prix / valeur) if (prix > 0 and valeur > 0) else None
    if ratio is not None:
        if ratio < float(r["prix_tres_bas_ratio"]):
            score += int(r["points_prix_tres_bas"])
            raisons.append(f"prix à {ratio:.0%} de la valeur du marché")
        elif ratio < float(r["prix_suspect_ratio"]):
            score += int(r["points_prix_suspect"])
            raisons.append(f"prix très inférieur au marché ({ratio:.0%})")
        if ratio < float(r["prix_suspect_ratio"]):
            if len((description or "").strip()) < 15:
                score += 10
                raisons.append("description quasi vide")
            if nb_photos == 0:
                score += 10
                raisons.append("aucune photo")
            if vendeur_avis is not None and \
                    vendeur_avis <= int(r["vendeur_peu_avis"]):
                score += int(r["points_vendeur_neuf_prix_bas"])
                raisons.append(f"vendeur à {vendeur_avis} avis seulement")

    score = max(0, min(100, int(score)))
    if score >= int(r["seuil_rejet"]):
        niveau = "eleve"
    elif score >= int(r["seuil_alerte"]):
        niveau = "moyen"
    else:
        niveau = "faible"
    return {"score": score, "niveau": niveau, "raisons": list(dict.fromkeys(raisons)),
            "rejeter": niveau == "eleve"}


def drapeau(verdict):
    """Ligne courte pour la notification (None si rien à signaler)."""
    if verdict["niveau"] == "faible":
        return None
    return (f"⚠️ risque d'arnaque {verdict['score']}/100 : "
            + ", ".join(verdict["raisons"][:3]) + " — vérifie avant de payer")
