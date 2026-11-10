"""
reseau.py — Sorties réseau et bascule automatique
═══════════════════════════════════════════════════════════════════════════
Vinted et Leboncoin (Datadome) bloquent par adresse IP. Une seule sortie
réseau, c'est un seul point de blocage. Ce module gère une CHAÎNE de
sorties, essayées dans l'ordre, et se souvient de celles qui sont bloquées :

    direct  → l'IP de la machine (ton Mac, ou le serveur GitHub)
    maison  → ton IP de box, via un appareil allumé chez toi (Tailscale, gratuit)
    warp    → Cloudflare WARP (gratuit, installé à la demande sur GitHub)
    proxy   → proxy(s) payant(s) éventuels, en dernier recours

Quand une sortie se fait bloquer, elle est mise de côté un moment (de plus
en plus longtemps si ça se répète) et la requête repart AUSSITÔT par la
suivante. L'état est écrit dans .sorties_etat.json : le passage suivant
(sur GitHub, un nouveau processus toutes les 10 min) repart directement de
la sortie qui marche, au lieu de griller des requêtes sur une IP bloquée —
ce qui aggraverait le blocage.

Variables d'environnement (toutes facultatives) :
    SNIPER_PROXY_MAISON   socks5h://127.0.0.1:1055   (posée par outils/sorties_secours.sh)
    SNIPER_PROXY_WARP     socks5h://127.0.0.1:40000  (idem)
    SNIPER_PROXY_URLS     proxys payants, séparés par virgules ou espaces
    SNIPER_PROXY_URL      un seul proxy (ancien nom, toujours accepté)
    SNIPER_DIRECT=0       ne jamais utiliser la connexion directe
    SNIPER_ORDRE_SORTIES  ex. "maison,direct,warp,proxy"
    SNIPER_ETAT_SORTIES   chemin du fichier d'état

Sans aucune variable : une seule sortie « direct », comportement d'origine.

    python3 reseau.py etat            # état des sorties (sans jamais afficher d'IP)
    python3 reseau.py besoin-secours  # code 0 si la sortie directe a été bloquée récemment
"""

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TYPES = ("direct", "maison", "warp", "proxy")

# Durée de mise à l'écart après 1, 2, 3… échecs d'affilée. La sortie directe
# est punie plus longtemps : une IP de serveur bloquée le reste souvent des
# heures, et chaque nouvel essai renforce sa mauvaise réputation.
PALIERS_S = {
    "direct": [900, 1800, 3600, 7200, 21600],     # 15 min → 6 h
    "defaut": [300, 900, 1800, 3600, 7200],       # 5 min → 2 h
}
# Quand TOUTES les sorties sont à l'écart : une seule sonde de temps en temps
# (celle qui se libère le plus tôt), jamais un martelage.
SONDE_INTERVALLE_S = 600
# « La sortie directe a été bloquée récemment » → installer les secours.
FENETRE_SECOURS_S = 6 * 3600

DEFAUT = object()   # sentinelle : « paramètre non fourni »


class SortiesEpuisees(Exception):
    """Toutes les sorties sont à l'écart et la sonde a déjà été utilisée."""


@dataclass(frozen=True)
class Sortie:
    nom: str
    type: str
    url: object = None          # None = connexion directe

    @property
    def directe(self):
        return self.url is None


def _decouper(valeur):
    return [v for v in re.split(r"[\s,;]+", valeur or "") if v]


def masquer(texte):
    """Retire les identifiants d'une URL de proxy avant de l'écrire dans un log
    (les logs d'un dépôt public sont publics)."""
    return re.sub(r"(//)[^/@\s]+@", r"\1***@", str(texte))


# ─────────────────────── LISTE DES SORTIES ───────────────────────
def sorties():
    """Sorties disponibles, dans l'ordre où les essayer."""
    env = os.environ
    par_type = {t: [] for t in TYPES}
    if env.get("SNIPER_DIRECT", "1").strip() != "0":
        par_type["direct"].append(Sortie("direct", "direct", None))
    maison = env.get("SNIPER_PROXY_MAISON", "").strip()
    if maison:
        par_type["maison"].append(Sortie("maison", "maison", maison))
    warp = env.get("SNIPER_PROXY_WARP", "").strip()
    if warp:
        par_type["warp"].append(Sortie("warp", "warp", warp))
    vus = []
    for url in _decouper(env.get("SNIPER_PROXY_URLS")) + _decouper(env.get("SNIPER_PROXY_URL")):
        if url not in vus:
            vus.append(url)
    for i, url in enumerate(vus, 1):
        par_type["proxy"].append(Sortie(f"proxy{i}", "proxy", url))

    ordre = [t for t in _decouper(env.get("SNIPER_ORDRE_SORTIES")) if t in TYPES]
    ordre += [t for t in TYPES if t not in ordre]
    liste = [s for t in ordre for s in par_type[t]]
    # Sans aucune sortie configurée (SNIPER_DIRECT=0 seul), on garde le direct :
    # mieux vaut une sortie peut-être bloquée qu'aucune.
    return liste or [Sortie("direct", "direct", None)]


# ─────────────────────── ÉTAT PERSISTANT ───────────────────────
class _Etat:
    def __init__(self):
        self.verrou = threading.RLock()
        self.data = None
        self.chemin = None
        self.cycle = {}      # source -> {"ok": nom de sortie | None, "echecs": n}
        self.sondes = {}     # source -> horodatage de la dernière sonde


_E = _Etat()


def chemin_etat():
    return os.environ.get("SNIPER_ETAT_SORTIES") or os.path.join(BASE_DIR, ".sorties_etat.json")


def _charger():
    chemin = chemin_etat()
    if _E.data is not None and _E.chemin == chemin:
        return _E.data
    data = {}
    try:
        with open(chemin, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, ValueError, OSError):
        data = {}                      # fichier absent ou abîmé : on repart à zéro
    for cle in ("sorties", "alertes", "vie"):
        if not isinstance(data.get(cle), dict):
            data[cle] = {}
    _E.data, _E.chemin = data, chemin
    return data


def sauver():
    """Écriture atomique : un arrêt brutal ne laisse jamais un JSON à moitié écrit."""
    with _E.verrou:
        data = _charger()
        chemin = _E.chemin
        try:
            dossier = os.path.dirname(chemin) or "."
            fd, tmp = tempfile.mkstemp(dir=dossier, prefix=".sorties_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, chemin)
        except OSError:
            pass                       # l'état est un confort, jamais une cause d'arrêt


def recharger():
    """Oublie l'état en mémoire (tests, ou changement de fichier)."""
    with _E.verrou:
        _E.data = None
        _E.cycle = {}
        _E.sondes = {}


def vie():
    """Petit espace persistant pour les tâches périodiques (signe de vie…)."""
    with _E.verrou:
        return _charger()["vie"]


def _fiche(source, nom):
    return _charger()["sorties"].setdefault(source, {}).setdefault(
        nom, {"echecs": 0, "bloque_jusqua": 0})


def bloquee(source, sortie, maintenant=None):
    fiche = _fiche(source, sortie.nom)
    return (maintenant or time.time()) < float(fiche.get("bloque_jusqua") or 0)


# ─────────────────────── CHOIX ET RETOURS ───────────────────────
def choisir(source, exclure=()):
    """La meilleure sortie pour `source`, hors `exclure` (déjà essayées).

    Première sortie libre dans l'ordre ; si toutes sont à l'écart, une sonde
    sur celle qui se libère le plus tôt (au plus une toutes les
    SONDE_INTERVALLE_S par source) ; sinon None.
    """
    with _E.verrou:
        maintenant = time.time()
        candidates = [s for s in sorties() if s.nom not in exclure]
        if not candidates:
            return None
        libres = [s for s in candidates if not bloquee(source, s, maintenant)]
        if libres:
            return libres[0]
        if maintenant - _E.sondes.get(source, 0) < SONDE_INTERVALLE_S:
            return None
        _E.sondes[source] = maintenant
        return min(candidates,
                   key=lambda s: float(_fiche(source, s.nom).get("bloque_jusqua") or 0))


def _cycle(source):
    return _E.cycle.setdefault(source, {"ok": None, "echecs": 0})


def signaler_succes(source, sortie):
    with _E.verrou:
        fiche = _fiche(source, sortie.nom)
        fiche.update(echecs=0, bloque_jusqua=0, dernier_succes=time.time())
        fiche.pop("erreur", None)
        _cycle(source)["ok"] = sortie.nom
        sauver()


def signaler_echec(source, sortie, erreur=""):
    """Met la sortie à l'écart. Retourne la durée de mise à l'écart (s)."""
    with _E.verrou:
        maintenant = time.time()
        fiche = _fiche(source, sortie.nom)
        n = int(fiche.get("echecs") or 0) + 1
        paliers = PALIERS_S.get(sortie.type, PALIERS_S["defaut"])
        delai = paliers[min(n - 1, len(paliers) - 1)]
        fiche.update(echecs=n, bloque_jusqua=maintenant + delai,
                     dernier_echec=maintenant, erreur=masquer(erreur)[:160])
        _cycle(source)["echecs"] += 1
        sauver()
        return delai


# ─────────────────────── BILAN D'UN CYCLE DE SCAN ───────────────────────
def debut_cycle(source=None):
    """Remet le bilan à zéro — pour une seule source en mode continu (chaque
    source a son propre fil et son propre rythme de bilan)."""
    with _E.verrou:
        if source is None:
            _E.cycle = {}
        else:
            _E.cycle.pop(source, None)


def bilan_cycle():
    """{source: {"ok": sortie qui a marché | None, "echecs": n}} du cycle en cours."""
    with _E.verrou:
        return {s: dict(c) for s, c in _E.cycle.items()}


def evaluer_alertes(source, seuil_bloque=2):
    """Ce qu'il faut annoncer pour `source` à la fin d'un cycle, ou None.

    ("bloque", None)   — toutes les sorties échouent depuis `seuil_bloque` cycles
    ("retabli", nom)   — ça remarche après une alerte « bloqué »
    ("secours", nom)   — le direct ne passe plus, une sortie de secours a pris
                         le relais (au plus une fois par 24 h : pas de spam
                         si le direct va et vient)

    Le seuil évite d'alerter pour un raté isolé.
    """
    with _E.verrou:
        c = _E.cycle.get(source)
        if not c or (not c["ok"] and not c["echecs"]):
            return None
        a = _charger()["alertes"].setdefault(source, {})
        a.setdefault("bloque_cycles", 0)
        a.setdefault("bloque_annonce", False)
        a.setdefault("secours_annonce_le", 0)
        maintenant = time.time()
        resultat = None
        if c["ok"]:
            precedente = a.get("sortie")
            a["bloque_cycles"] = 0
            a["sortie"] = c["ok"]
            direct_existe = any(s.directe for s in sorties())
            if a["bloque_annonce"]:
                a["bloque_annonce"] = False
                resultat = ("retabli", c["ok"])
            elif (c["ok"] != "direct" and direct_existe and precedente in (None, "direct")
                  and maintenant - float(a["secours_annonce_le"]) > 86400):
                a["secours_annonce_le"] = maintenant
                resultat = ("secours", c["ok"])
        else:
            a["bloque_cycles"] += 1
            if a["bloque_cycles"] >= max(1, int(seuil_bloque)) and not a["bloque_annonce"]:
                a["bloque_annonce"] = True
                resultat = ("bloque", None)
        sauver()
        return resultat


# ─────────────────────── LECTURE DE L'ÉTAT ───────────────────────
def resume(source=None):
    """État lisible des sorties, sans aucune URL ni IP."""
    with _E.verrou:
        maintenant = time.time()
        data = _charger()["sorties"]
        sources = [source] if source else sorted(data)
        out = {}
        for src in sources:
            lignes = []
            for s in sorties():
                f = data.get(src, {}).get(s.nom, {})
                reste = float(f.get("bloque_jusqua") or 0) - maintenant
                lignes.append({
                    "sortie": s.nom, "bloquee": reste > 0,
                    "reste_min": max(0, round(reste / 60)),
                    "echecs": int(f.get("echecs") or 0),
                    "dernier_succes": f.get("dernier_succes"),
                    "erreur": f.get("erreur") if reste > 0 else None})
            out[src] = lignes
        return out


def resume_texte(source):
    morceaux = []
    for l in resume(source).get(source, []):
        if l["bloquee"]:
            morceaux.append(f"{l['sortie']} ⛔ {l['reste_min']} min")
        elif l["dernier_succes"]:
            morceaux.append(f"{l['sortie']} ✅")
        else:
            morceaux.append(f"{l['sortie']} ·")
    return " · ".join(morceaux) or "aucune donnée"


def besoin_secours(maintenant=None):
    """Vrai si la sortie directe a été bloquée récemment, pour une source
    quelconque : c'est le signal pour installer les sorties de secours
    (WARP) au passage suivant, sans le faire à chaque fois pour rien."""
    with _E.verrou:
        maintenant = maintenant or time.time()
        for fiches in _charger()["sorties"].values():
            f = fiches.get("direct") or {}
            if float(f.get("bloque_jusqua") or 0) > maintenant:
                return True
            if maintenant - float(f.get("dernier_echec") or 0) < FENETRE_SECOURS_S:
                return True
        return False


# ─────────────────────── CLIENTS ───────────────────────
def proxy_url():
    """Proxy unique de l'ancien réglage (SNIPER_PROXY_URL). Utilisé par app.py,
    qui n'a pas de bascule : tout passe par ce proxy s'il est défini."""
    return (os.environ.get("SNIPER_PROXY_URL") or "").strip() or None


def proxies_http(url=DEFAUT):
    """Dict {"http": url, "https": url} pour requests / curl_cffi, ou None."""
    url = proxy_url() if url is DEFAUT else url
    return {"http": url, "https": url} if url else None


def proxy_lbc(url=DEFAUT):
    """Objet lbc.Proxy pour lbc.Client(proxy=...), ou None."""
    url = proxy_url() if url is DEFAUT else url
    if not url:
        return None
    import lbc
    p = urlparse(url)
    # Identifiants gardés tels quels (déjà encodés dans l'URL) : lbc.Proxy.url
    # les recolle sans les encoder, les décoder ici casserait l'URL finale.
    return lbc.Proxy(host=p.hostname, port=p.port or 80,
                     username=p.username or None, password=p.password or None,
                     scheme=p.scheme or "http")


def client_lbc(sortie=None, **kwargs):
    """lbc.Client passant par `sortie` (ou par l'ancien SNIPER_PROXY_URL).
    Attention : le constructeur fait déjà une requête (cookies) et peut lever."""
    import lbc
    url = sortie.url if sortie is not None else proxy_url()
    proxy = proxy_lbc(url)
    if proxy is not None:
        kwargs["proxy"] = proxy
    return lbc.Client(**kwargs)


if __name__ == "__main__":
    import sys
    commande = sys.argv[1] if len(sys.argv) > 1 else "etat"
    if commande == "besoin-secours":
        sys.exit(0 if besoin_secours() else 1)
    print("Sorties configurées :", ", ".join(s.nom for s in sorties()))
    for src, lignes in resume().items():
        print(f"\n{src}")
        for l in lignes:
            etat = f"⛔ à l'écart {l['reste_min']} min" if l["bloquee"] else "✅ disponible"
            print(f"  {l['sortie']:<8} {etat}  (échecs d'affilée : {l['echecs']})"
                  + (f"  — {l['erreur']}" if l["erreur"] else ""))
