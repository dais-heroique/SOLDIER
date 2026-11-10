"""
vendeur_avis.py — Filtre « le vendeur doit avoir au moins N avis »
═══════════════════════════════════════════════════════════════════════════
Un compte sans aucun avis est le profil classique de l'arnaque (annonce
alléchante, compte créé la veille). Ce module écarte ces annonces, sur
Vinted comme sur Leboncoin.

Principe : le nombre d'avis n'est pas dans les résultats de recherche, il
faut une requête par vendeur. On ne la fait donc QUE pour les annonces qui
ont survécu aux exclusions de périmètre (DDR3, SO-DIMM, PC complet…), et le
résultat est mis en cache : un vendeur qui a dix annonces n'est interrogé
qu'une fois.

Réglages (ram_config.yaml → filtre_vendeur, ou variable d'environnement) :
    actif       true
    avis_min    1          (SNIPER_MIN_REVIEWS=3 pour être plus strict)
    si_inconnu  rejeter    (rejeter | garder) — profil illisible ou sans id

⚠️  Ce filtre contredit volontairement l'ancien choix de ram_scoring.note_vendeur
(« un compte neuf n'est pas une mauvaise note ») : ici, zéro avis = écarté.
"""

import os
import random
import time

# Un profil vendeur change lentement : 6 h de cache suffisent largement.
TTL_S = 6 * 3600
# Après un échec de lecture, on ne réinterroge pas ce vendeur pendant 10 min.
TTL_ECHEC_S = 60     # un profil illisible est retenté vite : l'affaire n'attend pas 10 min
# Pause avant chaque requête profil (les tests la mettent à (0, 0)).
PAUSE_S = (0.3, 0.8)   # courte : c'est du temps pris sur l'alerte

_CACHE = {}    # (source, vendeur_id) -> (horodatage, info)
_ECHECS = {}   # (source, vendeur_id) -> (horodatage, message)


def reglages(cfg=None):
    """Réglages effectifs : défauts < ram_config.yaml < variable d'environnement."""
    actif, avis_min, si_inconnu = True, 1, "rejeter"
    if cfg is not None:
        actif = bool(cfg.val("filtre_vendeur.actif", True))
        try:
            avis_min = int(cfg.val("filtre_vendeur.avis_min", 1))
        except (TypeError, ValueError):
            avis_min = 1
        si_inconnu = str(cfg.val("filtre_vendeur.si_inconnu", "rejeter")).strip().lower()
    env = (os.environ.get("SNIPER_MIN_REVIEWS") or "").strip()
    if env:
        try:
            avis_min = int(env)
        except ValueError:
            pass
    return {"actif": actif and avis_min > 0, "avis_min": avis_min,
            "garder_inconnu": si_inconnu == "garder"}


def vider_cache():
    _CACHE.clear()
    _ECHECS.clear()


def _info_leboncoin(client, vendeur_id):
    u = client.get_user(vendeur_id)
    fb = getattr(u, "feedback", None)
    avis = getattr(fb, "received_count", None)
    # Compte professionnel : les avis peuvent être portés par la fiche boutique.
    if avis is None and getattr(u, "pro", None) is not None:
        avis = getattr(getattr(u.pro, "rating", None), "user_ratings_total", None)
    note = getattr(fb, "score", None) if fb is not None else None
    # Profil lu sans compteur = aucun avis reçu. Le script diag_vendeur.py
    # affiche la valeur brute pour vérifier ce point sur de vraies annonces.
    return {"avis": int(avis) if avis is not None else 0,
            "note": round(float(note), 2) if note else None,
            "pseudo": getattr(u, "name", None)}


def lire_avis(source, client, vendeur_id):
    """{"avis": int, "note": float|None, "pseudo": str|None}. Lève en cas d'échec."""
    cle = (source, str(vendeur_id))
    maintenant = time.time()

    echec = _ECHECS.get(cle)
    if echec and maintenant - echec[0] < TTL_ECHEC_S:
        raise RuntimeError(echec[1])
    hit = _CACHE.get(cle)
    if hit and maintenant - hit[0] < TTL_S:
        return hit[1]

    lo, hi = PAUSE_S
    if hi > 0:
        time.sleep(random.uniform(lo, hi))
    try:
        if source == "vinted":
            info = client.get_user_feedback(vendeur_id)
        elif source == "leboncoin":
            info = _info_leboncoin(client, vendeur_id)
        else:
            raise ValueError(f"source sans lecture d'avis : {source}")
    except Exception as e:
        _ECHECS[cle] = (time.time(), f"{type(e).__name__}: {e}"[:200])
        raise
    _CACHE[cle] = (time.time(), info)
    return info


def evaluer(source, client, vendeur_id, cfg=None):
    """Décide si le vendeur est acceptable.

    Retourne un dict :
        ok          bool   — l'annonce peut continuer
        avis        int|None
        note        float|None   (/5)
        pseudo      str|None
        motif       str|None     — raison du rejet
        transitoire bool   — échec de lecture (réseau, 403…) : à retenter plus tard
    """
    r = reglages(cfg)
    if not r["actif"]:
        return {"ok": True, "avis": None, "note": None, "pseudo": None,
                "motif": None, "transitoire": False}

    def inconnu(raison, transitoire):
        return {"ok": r["garder_inconnu"], "avis": None, "note": None, "pseudo": None,
                "motif": None if r["garder_inconnu"] else f"vendeur : {raison}",
                "transitoire": transitoire}

    if not vendeur_id:
        return inconnu("identifiant vendeur absent", False)
    try:
        info = lire_avis(source, client, vendeur_id)
    except Exception as e:
        return inconnu(f"avis illisibles ({type(e).__name__})", True)

    avis = int(info.get("avis") or 0)
    ok = avis >= r["avis_min"]
    return {"ok": ok, "avis": avis, "note": info.get("note"),
            "pseudo": info.get("pseudo"), "transitoire": False,
            "motif": None if ok else
            f"vendeur avec {avis} avis (minimum {r['avis_min']})"}
