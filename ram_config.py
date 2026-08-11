"""
ram_config.py — Chargement des paramètres métier (YAML) et des secrets (.env)
═══════════════════════════════════════════════════════════════════════════
Deux sources, deux natures :
  • ram_config.yaml — seuils, marges, quotas, mots-clés, notif_mode.
    Éditable à chaud : `get()` recharge le fichier si son mtime a bougé
    (contrôlé toutes les RELOAD_INTERVAL secondes). Pas de redémarrage à faire.
  • .env — token Telegram, clé Gemini. Jamais commité, jamais loggé.

Toute lecture passe par un chemin pointé :
    cfg = ram_config.get()
    cfg.val("scoring.seuil_notification", 65)
Une clé absente du YAML retombe sur le défaut fourni : un fichier de config
incomplet (ou une ancienne version) ne fait jamais planter un worker.
"""

import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("RAM_CONFIG", os.path.join(BASE_DIR, "ram_config.yaml"))
# Surcharges locales à la machine, JAMAIS versionnées. C'est ici qu'écrit
# ram_setup.py : modifier ram_config.yaml (qui est suivi par git) ferait échouer
# chaque « git pull » avec « your local changes would be overwritten ».
LOCAL_FILE = os.path.join(BASE_DIR, "ram_config.local.yaml")
ENV_FILE = os.path.join(BASE_DIR, ".env")
RELOAD_INTERVAL = 30.0

try:
    import yaml
    _HAS_YAML = True
except ImportError:                                    # pragma: no cover
    _HAS_YAML = False


# ─────────────────────── DÉFAUTS ───────────────────────
# Le YAML est la référence ; ces valeurs ne servent que si une clé manque.
DEFAUTS = {
    "scoring": {
        "seuil_notification": 55, "seuil_vision": 45, "seuil_confirme": 70,
        "marge_min_eur": 20, "marge_min_pct": 45,
        "poids_pre_score": {"marge": 0.50, "liquidite": 0.25,
                            "qualite_annonce": 0.15, "vendeur": 0.10},
        "poids_score_final": {"marge": 0.40, "liquidite": 0.20,
                              "confiance_vision": 0.20, "vendeur": 0.10,
                              "logistique": 0.10},
        "marge_plafond_eur": 60, "marge_pct_plafond": 100,
        "confiance_probable_min": 0.50, "confiance_confirme_min": 0.75,
    },
    "frais": {
        "vinted": {"protection_pct": 5.0, "protection_fixe": 0.70,
                   "port_defaut": 3.50, "port_min": 2.0, "port_max": 5.0},
        "leboncoin": {"protection_pct": 0.0, "protection_fixe": 0.0,
                      "port_defaut": 4.50},
        "revente": {"vinted_commission_pct": 0.0, "leboncoin_commission_pct": 0.0,
                    "emballage_eur": 1.20},
    },
    "perimetre": {
        "capacites_autorisees": [8, 16, 32], "frequence_min": 2133,
        "accepter_pc_complets": False,
        "exception_4go": {"actif": True, "nb_min": 10, "prix_max_unitaire": 1.50},
        "exclusions": {"sodimm": [], "ecc": [], "generation": []},
        "pieges_ddr3": {"mode": "degrader", "penalite_score": 20,
                        "plateformes": [], "modeles": []},
    },
    "multiplicateurs": {
        "kit_assorti_origine": 1.25, "rgb": 1.20, "couleur_blanche": 1.15,
        "low_profile": 1.10, "memtest_prouve": 1.10, "dual_rank_2x16": 1.05,
        "dissipateur_manquant": 0.75, "sans_boite": 0.95, "no_name": 0.50,
        "rotation_no_name_facteur": 3.0,
    },
    "vision": {
        "actif": True, "provider": "gemini", "modele": "gemini-2.0-flash",
        "quota": {"par_minute": 10, "par_jour": 200, "marge_securite": 0.9},
        "max_photos": 3, "photo_max_px": 1024, "photo_max_octets": 3500000,
        "timeout_s": 25, "max_tentatives": 2, "reprise_differees_max": 3,
        "temperature": 0.1,
    },
    "telegram": {
        "actif": True, "notif_mode": "edit", "anti_spam_s": 60, "rafale_max": 4, "election_leader": True,
        "notifier_rejets": False, "notifier_appariements": True,
        "message_demande_photo": "Bonjour, pourriez-vous m'envoyer une photo "
                                 "du sticker de la barrette ? Merci !",
    },
    "appariement": {
        "actif": True, "marge_kit_min_eur": 30, "bonus_kit_pct": 25,
        "statuts_stock_eligibles": ["recu", "en_test", "teste_ok", "liste"],
        "autoriser_specs_seules": False,
    },
    "calibrage": {
        "actif": True, "heure_job": "04:30", "fenetre_jours": 30,
        "min_observations": 3, "methode": "mediane", "alerte_perime_jours": 14,
        "variation_max_pct": 40,
    },
    "run": {
        "dry_run": False, "log_json": True, "log_niveau": "INFO",
        "purge_annonces_jours": 90, "revalider_annonces_h": 6,
    },
    "dashboard": {"alerte_capital_dormant_pct": 40, "feed_heures": 24, "feed_max": 200},
    "sources": {
        "vinted": {"actif": True, "mots_cles": [], "delai_par_mot_cle_s": [30, 45],
                   "delai_entre_requetes_s": [2.5, 5.0],
                   "max_resultats_par_requete": 20,
                   "backoff_429_s": [60, 120, 300, 600]},
        "leboncoin": {"actif": True, "mots_cles": [], "departements": [],
                      "delai_par_mot_cle_s": [60, 90],
                      "delai_entre_requetes_s": [5.0, 9.0],
                      "max_resultats_par_requete": 35,
                      "backoff_429_s": [120, 300, 900, 1800]},
    },
}


def _fusion(base, sur):
    """Fusion récursive : le YAML surcharge les défauts clé par clé, sans
    faire disparaître les sous-clés qu'il ne mentionne pas."""
    out = dict(base)
    for k, v in (sur or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _fusion(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data, chemin=None, mtime=None, erreur=None):
        self.data = data
        self.chemin = chemin
        self.mtime = mtime
        self.erreur = erreur

    def val(self, chemin_pointe, defaut=None):
        """cfg.val("vision.quota.par_minute", 10)"""
        noeud = self.data
        for morceau in chemin_pointe.split("."):
            if not isinstance(noeud, dict) or morceau not in noeud:
                return defaut
            noeud = noeud[morceau]
        return defaut if noeud is None else noeud

    def section(self, nom):
        return self.val(nom, {}) or {}

    # Raccourcis des valeurs les plus lues, pour éviter les chaînes magiques
    # dispersées dans les workers.
    @property
    def dry_run(self):
        return bool(os.environ.get("RAM_DRY_RUN")) or bool(self.val("run.dry_run", False))

    @property
    def notif_mode(self):
        mode = str(self.val("telegram.notif_mode", "edit")).strip().lower()
        return mode if mode in ("edit", "second_message") else "edit"

    def quota_vision(self):
        """Plafonds effectifs, marge de sécurité appliquée. Le quota gratuit
        Google évolue : on ne colle jamais au plafond annoncé."""
        marge = float(self.val("vision.quota.marge_securite", 0.9) or 0.9)
        minute = int(self.val("vision.quota.par_minute", 10) or 0)
        jour = int(self.val("vision.quota.par_jour", 200) or 0)
        return max(1, int(minute * marge)), max(1, int(jour * marge))


_lock = threading.Lock()
_cache = {"config": None, "verifie_le": 0.0}


def _mtimes():
    """Empreinte des deux fichiers de config, pour détecter un changement."""
    out = []
    for chemin in (CONFIG_FILE, LOCAL_FILE):
        try:
            out.append(os.path.getmtime(chemin))
        except OSError:
            out.append(None)
    return tuple(out)


def charger_local():
    """Surcharges locales (ram_config.local.yaml). Absent = dict vide."""
    if not _HAS_YAML:
        return {}
    try:
        with open(LOCAL_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _charger():
    if not _HAS_YAML:
        return Config(DEFAUTS, erreur="PyYAML absent : défauts intégrés utilisés "
                                      "(pip install pyyaml)")
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            brut = yaml.safe_load(f) or {}
        # Ordre de priorité : défauts < ram_config.yaml < ram_config.local.yaml
        fusionne = _fusion(_fusion(DEFAUTS, brut), charger_local())
        return Config(fusionne, CONFIG_FILE, _mtimes())
    except FileNotFoundError:
        return Config(_fusion(DEFAUTS, charger_local()),
                      erreur=f"{CONFIG_FILE} introuvable : défauts utilisés")
    except Exception as e:
        # Un YAML cassé ne doit JAMAIS arrêter les scrapers en cours : on garde
        # la dernière config valide et on remonte l'erreur au dashboard.
        precedent = _cache.get("config")
        if precedent is not None:
            precedent.erreur = f"YAML invalide, ancienne config conservée : {e}"
            return precedent
        return Config(DEFAUTS, erreur=f"YAML invalide : {e}")


def get(force=False):
    """Config courante, rechargée si le fichier a changé (au plus une
    vérification toutes les RELOAD_INTERVAL secondes)."""
    now = time.time()
    with _lock:
        cfg = _cache["config"]
        if cfg is None or force or (now - _cache["verifie_le"]) > RELOAD_INTERVAL:
            _cache["verifie_le"] = now
            if cfg is None or force:
                _cache["config"] = _charger()
            elif _mtimes() != cfg.mtime:
                _cache["config"] = _charger()
        return _cache["config"]


# ─────────────────────── SECRETS (.env) ───────────────────────
def _lire_env():
    """Parseur .env minimal (pas de dépendance python-dotenv). Les variables
    d'environnement déjà définies gagnent toujours sur le fichier."""
    valeurs = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#") or "=" not in ligne:
                    continue
                cle, _, valeur = ligne.partition("=")
                valeur = valeur.strip().strip('"').strip("'")
                valeurs[cle.strip()] = valeur
    except FileNotFoundError:
        pass
    return valeurs


_env_cache = None


def secret(nom, defaut=None):
    global _env_cache
    if nom in os.environ and os.environ[nom]:
        return os.environ[nom]
    if _env_cache is None:
        _env_cache = _lire_env()
    return _env_cache.get(nom, defaut)


def secrets_manquants():
    """Ce qu'il manque pour tourner en conditions réelles. Affiché au démarrage
    et sur le dashboard, plutôt que de découvrir l'absence de clé au moment où
    une affaire à 60 € de marge passe."""
    manquants = []
    cfg = get()
    if cfg.val("telegram.actif", True):
        if not secret("TELEGRAM_BOT_TOKEN"):
            manquants.append("TELEGRAM_BOT_TOKEN")
        if not secret("TELEGRAM_CHAT_ID"):
            manquants.append("TELEGRAM_CHAT_ID")
    if cfg.val("vision.actif", True) and not secret("GEMINI_API_KEY"):
        manquants.append("GEMINI_API_KEY")
    return manquants


if __name__ == "__main__":
    import json
    cfg = get()
    print(f"Config : {cfg.chemin or '(défauts)'}")
    if cfg.erreur:
        print(f"⚠️  {cfg.erreur}")
    print(f"notif_mode          : {cfg.notif_mode}")
    print(f"dry_run             : {cfg.dry_run}")
    print(f"quota vision        : {cfg.quota_vision()} (minute, jour)")
    print(f"seuils              : notif≥{cfg.val('scoring.seuil_notification')} "
          f"vision≥{cfg.val('scoring.seuil_vision')} "
          f"confirmé≥{cfg.val('scoring.seuil_confirme')}")
    print(f"marge min           : {cfg.val('scoring.marge_min_eur')}€ ET "
          f"{cfg.val('scoring.marge_min_pct')}%")
    print(f"mots-clés Vinted    : {len(cfg.val('sources.vinted.mots_cles', []))}")
    print(f"mots-clés Leboncoin : {len(cfg.val('sources.leboncoin.mots_cles', []))}")
    manque = secrets_manquants()
    print(f"secrets manquants   : {', '.join(manque) if manque else 'aucun ✅'}")
    if "--dump" in os.sys.argv:
        print(json.dumps(cfg.data, indent=2, ensure_ascii=False))
