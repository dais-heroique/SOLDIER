"""
vinted_age.py — Date de publication ESTIMÉE d'une annonce Vinted
═══════════════════════════════════════════════════════════════════════════
La page de recherche Vinted ne donne plus de date. Or l'âge compte : une
annonce publiée il y a 30 secondes est une cible, la même vieille de dix
jours a déjà été vue par tout le monde.

Les identifiants Vinted sont attribués dans l'ordre de création. Le bot
interroge « plus récent d'abord » toutes les ~10 s : quand un identifiant plus
grand que tous les précédents apparaît, l'annonce vient d'être publiée. Ces
couples (heure, identifiant) servent de repères ; l'âge de n'importe quelle
autre annonce se lit par interpolation entre deux repères.

C'est une ESTIMATION : exacte à quelques secondes pour une annonce toute
fraîche, à quelques dizaines de minutes pour une annonce de la nuit, et
grossière au-delà du plus vieux repère (extrapolée au rythme moyen). Sans
repères suffisants, on ne dit rien (None) plutôt que d'inventer.
"""

import threading
import time

import reseau

MAX_REPERES = 400          # ~13 h de repères à un toutes les 2 min
ECART_MIN_S = 120          # au plus un repère enregistré toutes les 2 min
DUREE_MIN_TAUX_S = 1800    # pas d'extrapolation avant 30 min de recul
AGE_MAX_S = 30 * 86400

_verrou = threading.Lock()
_etat = {"base": None, "vu": None}   # base : plus grand id au démarrage (non daté)


def reinitialiser():
    with _verrou:
        _etat.update(base=None, vu=None)


def _reperes():
    return reseau.vie().setdefault("vinted_reperes", [])


def _entier(valeur):
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return None


def observer(ids, maintenant=None):
    """À appeler après chaque recherche Vinted, avec les identifiants rendus."""
    maintenant = float(maintenant or time.time())
    connus = [i for i in (_entier(x) for x in ids) if i]
    if not connus:
        return
    plus_grand = max(connus)
    with _verrou:
        reperes = _reperes()
        plancher = max([_etat["base"] or 0, _etat["vu"][1] if _etat["vu"] else 0]
                       + [int(r[1]) for r in reperes[-1:]])
        if _etat["base"] is None:
            # Première recherche depuis le démarrage : l'annonce la plus
            # récente peut dater d'avant (la nuit, une coupure). On ne la date pas.
            _etat["base"] = max(plus_grand, plancher)
            return
        if plus_grand <= plancher:
            return
        _etat["vu"] = (maintenant, plus_grand)     # vue pour la première fois : publiée à l'instant
        if not reperes or maintenant - float(reperes[-1][0]) >= ECART_MIN_S:
            reperes.append([round(maintenant, 1), plus_grand])
            del reperes[:-MAX_REPERES]


def estimer(identifiant, maintenant=None):
    """Horodatage de publication estimé, ou None si on ne peut pas savoir."""
    ident = _entier(identifiant)
    if not ident:
        return None
    maintenant = float(maintenant or time.time())
    with _verrou:
        points = [(float(t), int(i)) for t, i in _reperes()]
        vu = _etat["vu"]
    if vu and (not points or vu[1] > points[-1][1]):
        points.append((float(vu[0]), int(vu[1])))
    if len(points) < 2:
        return None
    if ident >= points[-1][1]:
        return min(maintenant, points[-1][0]) if ident == points[-1][1] else None
    for (t0, i0), (t1, i1) in zip(points, points[1:]):
        if i0 <= ident <= i1 and i1 > i0 and t1 >= t0:
            return t0 + (t1 - t0) * (ident - i0) / (i1 - i0)
    # Plus vieille que le plus vieux repère : rythme moyen, si le recul suffit.
    (t0, i0), (t1, i1) = points[0], points[-1]
    if t1 - t0 < DUREE_MIN_TAUX_S or i1 <= i0:
        return None
    estimation = t0 - (i0 - ident) * (t1 - t0) / (i1 - i0)
    return max(estimation, maintenant - AGE_MAX_S)
